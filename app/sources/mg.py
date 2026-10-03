"""MG Approved Used source using MG UK's vehicle-locator JSON endpoint."""
from __future__ import annotations
import json, random, re, time
from typing import Any
import requests
try:
    from curl_cffi import requests as cffi_requests
except ImportError: cffi_requests=None
from app.db import now_iso
from app.geo import distance_miles, place_location, postcode_location
from app.sources import SearchResult, body_and_seats_match, classify_body_type, fuel_matches, normalise_plate, standardise, transmission_matches
from app.sources.generic_used import find_vehicle_records, first_value, int_from
SOURCE_KEY="mg";SOURCE_NAME="MG Approved Used";MAKES=("MG",)
SITE_ROOT="https://www.mg.co.uk";SEARCH_PAGE=SITE_ROOT+"/used-cars";API="https://mg-vl.vehicle-locator.net/ajax/ajax-vehicles.php"
MAX_PAGES=100;PAGE_DELAY=.5;PAGE_JITTER=.4;IMPERSONATE="chrome";_s=None

def _plain(v):return re.sub(r"[^a-z0-9]+","",str(v or "").lower())
def _http():
 global _s
 if _s is None:_s=cffi_requests.Session(impersonate=IMPERSONATE) if cffi_requests is not None else requests.Session()
 return _s

def _fetch(page:int):
 h={"Accept":"application/json,text/plain,*/*","Referer":SEARCH_PAGE,"Origin":SITE_ROOT}
 r=_http().get(API,params={"page":page},headers=h,timeout=60)
 if r.status_code in (400,405):r=_http().post(API,data={"page":page},headers=h,timeout=60)
 r.raise_for_status()
 try:return r.json()
 except ValueError:
  # Some locator versions return a JSON string wrapped in HTML/text.
  m=re.search(r"([\[{].*[\]}])",r.text,re.S)
  if not m:raise RuntimeError("MG vehicle locator returned non-JSON data")
  return json.loads(m.group(1))
def _rec(o):
 reg=normalise_plate(first_value(o,"registration","registrationNumber","regNumber","regNo","vrm","plate"))
 if not reg:return None
 model=first_value(o,"model","modelName","range","vehicleModel");title=str(first_value(o,"title","displayName","vehicleName") or "")
 first=str(first_value(o,"firstRegistrationDate","registrationDate","registeredDate","dateRegistered") or "")[:10] or None
 n=first_value(o,"images","photos","imageList","photoCount","imageCount"); n=len(n) if isinstance(n,list) else int_from(n)
 url=first_value(o,"url","detailUrl","vehicleUrl","href");url=SITE_ROOT+url if isinstance(url,str) and url.startswith("/") else url
 return {"registration":reg,"make":"MG","model":str(model) if model else None,"trim":first_value(o,"trim","variant","derivative","version"),"title":title,
 "year":int_from(first[:4]) if first else int_from(first_value(o,"year","registrationYear","modelYear")),"first_registered":first,
 "price":int_from(first_value(o,"price","cashPrice","sellingPrice","vehiclePrice")),"mileage":int_from(first_value(o,"mileage","miles","odometer")),
 "fuel":first_value(o,"fuel","fuelType"),"transmission":first_value(o,"transmission","gearbox"),"colour":first_value(o,"colour","color","exteriorColour"),
 "body_type":classify_body_type(first_value(o,"bodyType","bodyStyle"),model,title),"seats":int_from(first_value(o,"seats","seatCount")),"photo_count":n,
 "dealer":first_value(o,"dealerName","retailerName","dealer"),"location":first_value(o,"town","city","dealerTown","location"),"url":url or SEARCH_PAGE,"raw":o}
def _wanted(c,car):
 if car.get("model") and _plain(car["model"]) not in _plain(f"{c.get('model')} {c.get('title')}"):return False
 if not fuel_matches(str(car.get("fuel") or "Any"),str(c.get("fuel") or "")):return False
 if not transmission_matches(str(car.get("transmission") or "Any"),str(c.get("transmission") or "")):return False
 checks=(("price","price_min",lambda a,b:a<b),("price","price_max",lambda a,b:a>b),("mileage","mileage_max",lambda a,b:a>b),("year","year_min",lambda a,b:a<b))
 return not any(c.get(f) is not None and car.get(k) is not None and op(int(c[f]),int(car[k])) for f,k,op in checks)
def _row(c,home):
 loc=place_location(str(c.get("location"))) if c.get("location") else None;n=c.get("photo_count")
 return standardise({**{k:c.get(k) for k in ("registration","make","model","trim","year","first_registered","colour","fuel","transmission","body_type","seats","mileage","price","dealer","location","url","title")},"previous_price":None,
 "distance_miles":distance_miles(home,loc) if loc else None,"photo_status":"photos" if n and n>1 else "awaiting" if n is not None else "unknown","photo_count":n,"photo_reason":f"{n} dealer images" if n else None,
 "raw_text":json.dumps(c.get("raw"),ensure_ascii=False,default=str),"source":SOURCE_KEY,"status":"active","last_seen":now_iso()})
def search(car,settings,timings=None):
 if _plain(car.get("make"))!="mg":raise RuntimeError(f"MG source does not search make '{car.get('make')}'")
 result=SearchResult(search_url=SEARCH_PAGE);home=postcode_location(settings.get("home_postcode"));seen=set();logs=[]
 for page in range(1,MAX_PAGES+1):
  if page>1:time.sleep(PAGE_DELAY+random.uniform(0,PAGE_JITTER))
  st=time.perf_counter();data=_fetch(page);secs=time.perf_counter()-st;records=[]
  for o in find_vehicle_records(data):
   c=_rec(o)
   if c and c["registration"] not in {x["registration"] for x in records}:records.append(c)
  new=[c for c in records if c["registration"] not in seen];added=0
  for c in new:
   seen.add(c["registration"]);result.raw_regs.add(c["registration"])
   if _wanted(c,car) and body_and_seats_match(car,c.get("body_type"),c.get("seats")):result.rows.append(_row(c,home));added+=1
  logs.append({"search":car.get("name"),"page":page,"fetch_seconds":round(secs,3),"vehicle_objects":len(records),"rows":added})
  if not new:break
 if timings is not None:timings["search_fetch_seconds"]=round(float(timings.get("search_fetch_seconds") or 0)+sum(x["fetch_seconds"] for x in logs),3);timings.setdefault("pages",[]).extend(logs)
 result.debug_text="\n".join(json.dumps(x) for x in logs);return result
