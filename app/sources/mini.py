"""MINI Approved Used source.

The MINI UK locator exposes public JSON under /vehicle/api/.  The exact nesting
has changed between site releases, so records are located by their UK
registration and field aliases rather than a single brittle JSON path.
"""
from __future__ import annotations
import json, random, re, time
from typing import Any
import requests
try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests=None
from app.db import now_iso
from app.geo import distance_miles, place_location, postcode_location
from app.sources import SearchResult, body_and_seats_match, classify_body_type, fuel_matches, normalise_plate, standardise, transmission_matches
from app.sources.generic_used import find_vehicle_records, first_value, int_from

SOURCE_KEY="mini"; SOURCE_NAME="MINI Approved Used"; MAKES=("MINI",)
SITE_ROOT="https://approvedusedminis.co.uk"; API=SITE_ROOT+"/vehicle/api/list/"; MODELS_API=SITE_ROOT+"/vehicle/api/models/"
PAGE_DELAY=.4; PAGE_JITTER=.3; MAX_PAGES=100; IMPERSONATE="chrome"
HEADERS={"Accept":"application/json","Referer":SITE_ROOT+"/result/","User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
_s=None

def _plain(v:Any)->str:return re.sub(r"[^a-z0-9]+","",str(v or "").lower())
def _http():
    global _s
    if _s is None:
        if cffi_requests is not None:_s=cffi_requests.Session(impersonate=IMPERSONATE)
        else:_s=requests.Session();_s.headers.update(HEADERS)
    return _s

def _get(url:str,params:dict[str,Any]|None=None)->Any:
    h={"Accept":"application/json","Referer":HEADERS["Referer"]} if cffi_requests is not None else None
    r=_http().get(url,params=params,headers=h,timeout=60);r.raise_for_status();return r.json()

def _model_code(model:str)->str|None:
    try:data=_get(MODELS_API)
    except Exception:return None
    wanted=_plain(model)
    for obj in find_vehicle_records(data):  # unusual but harmless if model endpoint contains vehicles
        pass
    def walk(x):
        if isinstance(x,dict):
            name=next((v for k,v in x.items() if _plain(k) in {"name","label","model","description"} and isinstance(v,str)),None)
            code=next((v for k,v in x.items() if _plain(k) in {"code","value","id","slug","modelcode"} and isinstance(v,(str,int))),None)
            if name and code and wanted in _plain(name): return str(code)
            for v in x.values():
                r=walk(v)
                if r:return r
        elif isinstance(x,list):
            for v in x:
                r=walk(v)
                if r:return r
        return None
    return walk(data)

def _record(obj:dict[str,Any])->dict[str,Any]|None:
    reg=normalise_plate(first_value(obj,"registration","registrationNumber","registrationPlate","regNumber","regNo","vrm","plate"))
    if not reg:return None
    title=str(first_value(obj,"title","vehicleTitle","displayName","name") or "")
    model=str(first_value(obj,"model","modelName","modelDescription") or "").strip() or None
    if model and _plain(model).startswith("mini") and len(model)>4:model=model[4:].strip()
    trim=first_value(obj,"trim","variant","derivative","version","modelVariant")
    first=str(first_value(obj,"registrationDate","firstRegistrationDate","firstRegistered","dateFirstRegistered") or "")[:10] or None
    year=int_from(first[:4]) if first else int_from(first_value(obj,"year","registrationYear","modelYear"))
    price=int_from(first_value(obj,"price","cashPrice","sellingPrice","vehiclePrice","priceValue"))
    mileage=int_from(first_value(obj,"mileage","odometer","miles","odometerMiles"))
    fuel=first_value(obj,"fuel","fuelType","fuelDescription")
    gear=first_value(obj,"transmission","gearbox","transmissionType")
    colour=first_value(obj,"colour","color","exteriorColour","exteriorColor")
    body=classify_body_type(first_value(obj,"bodyType","bodyStyle","bodystyle"),model,title)
    photos=first_value(obj,"photoCount","imageCount","imagesCount","numberOfImages")
    if isinstance(photos,list):photos=len(photos)
    photos=int_from(photos)
    dealer=first_value(obj,"dealerName","retailerName","locationName","dealer","retailer")
    town=first_value(obj,"town","city","dealerTown","retailerTown","location")
    url=first_value(obj,"url","vehicleUrl","detailUrl","href")
    if isinstance(url,str) and url.startswith("/"):url=SITE_ROOT+url
    return {"registration":reg,"make":"MINI","model":model,"trim":str(trim).strip() if trim else None,"title":title,"year":year,"first_registered":first,
            "price":price,"mileage":mileage,"fuel":str(fuel) if fuel else None,"transmission":str(gear) if gear else None,"colour":str(colour) if colour else None,
            "body_type":body,"seats":int_from(first_value(obj,"seats","seatCount")),"photo_count":photos,"dealer":str(dealer) if dealer else None,"location":str(town) if town else None,
            "url":str(url) if url else SITE_ROOT+"/result/","raw":obj}

def _wanted(c,car):
    if car.get("model") and _plain(car["model"]) not in _plain(f"{c.get('model')} {c.get('title')}"):return False
    if not fuel_matches(str(car.get("fuel") or "Any"),str(c.get("fuel") or "")):return False
    if not transmission_matches(str(car.get("transmission") or "Any"),str(c.get("transmission") or "")):return False
    for field,key,op in (("price","price_min","min"),("price","price_max","max"),("mileage","mileage_max","max"),("year","year_min","min")):
        v,w=c.get(field),car.get(key)
        if v is not None and w is not None and ((op=="min" and int(v)<int(w)) or (op=="max" and int(v)>int(w))):return False
    return True

def _row(c,home):
    loc=place_location(c.get("location")) if c.get("location") else None; n=c.get("photo_count")
    return standardise({**{k:c.get(k) for k in ("registration","make","model","trim","year","first_registered","colour","fuel","transmission","body_type","seats","mileage","price","dealer","location","url","title")},
        "previous_price":None,"distance_miles":distance_miles(home,loc) if loc else None,"photo_status":"photos" if n and n>1 else "awaiting" if n is not None else "unknown",
        "photo_count":n,"photo_reason":f"{n} dealer images" if n else None,"raw_text":json.dumps(c.get("raw"),ensure_ascii=False,default=str),"source":SOURCE_KEY,"status":"active","last_seen":now_iso()})

def search(car,settings,timings=None):
    if _plain(car.get("make"))!="mini":raise RuntimeError(f"MINI source does not search make '{car.get('make')}'")
    base={}; code=_model_code(str(car.get("model") or "")) if car.get("model") else None
    if code:base["model"]=code
    result=SearchResult(search_url=SITE_ROOT+"/result/");home=postcode_location(settings.get("home_postcode"));seen=set();logs=[]
    for page in range(1,MAX_PAGES+1):
        if page>1:time.sleep(PAGE_DELAY+random.uniform(0,PAGE_JITTER))
        params=dict(base);params["page"]=page;started=time.perf_counter()
        data=_get(API,params);secs=time.perf_counter()-started;records=[]
        for obj in find_vehicle_records(data):
            c=_record(obj)
            if c and c["registration"] not in {x["registration"] for x in records}:records.append(c)
        new=[c for c in records if c["registration"] not in seen];added=0
        for c in new:
            seen.add(c["registration"]);result.raw_regs.add(c["registration"])
            if _wanted(c,car) and body_and_seats_match(car,c.get("body_type"),c.get("seats")):result.rows.append(_row(c,home));added+=1
        logs.append({"search":car.get("name"),"page":page,"fetch_seconds":round(secs,3),"vehicle_objects":len(records),"rows":added})
        if not new:break
    if timings is not None:timings["search_fetch_seconds"]=round(float(timings.get("search_fetch_seconds") or 0)+sum(x["fetch_seconds"] for x in logs),3);timings.setdefault("pages",[]).extend(logs)
    result.debug_text="\n".join(json.dumps(x) for x in logs);return result
