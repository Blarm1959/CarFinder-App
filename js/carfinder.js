const API="/api/v1",$=id=>document.getElementById(id);let manufacturers=[],profiles=[],currentProfile=null,presets=[],settings={},searchModels={},presetModels={},modelEditorTarget="search",resultRows=[],resultView="unreviewed",resultSorts=[{key:"price",asc:true},{key:"",asc:true},{key:"",asc:true}],searchInProgress=false;const COLOURS=["Black","Blue","Brown","Grey","Green","Orange","Red","Silver","White","Yellow","Beige","Gold","Purple"];
async function api(path,options={}){const r=await fetch(API+path,{credentials:"same-origin",headers:{"Content-Type":"application/json"},...options});let body={};try{body=await r.json()}catch{}if(r.status===401){showLogin();throw Error(body.detail||"Choose your name")}if(!r.ok)throw Error(body.detail||`Request failed (${r.status})`);return body}
async function loadVersion(){try{const b=await api("/health");const v=b.version;if(v)$("versionButton").textContent=String(v).startsWith("v")?v:`v${v}`}catch{}}
function showLogin(){$("loginView").classList.remove("hidden");$("appView").classList.add("hidden")}function showApp(){$("loginView").classList.add("hidden");$("appView").classList.remove("hidden")}function page(name){document.querySelectorAll(".page").forEach(x=>x.classList.toggle("active",x.id===`page-${name}`));document.querySelectorAll(".nav-item").forEach(x=>x.classList.toggle("active",x.dataset.page===name));scrollTo(0,0)}
const num=v=>v===""||v==null?null:Number(v),val=(id,v)=>$(id).value=v??"",esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));

async function boot(){
  loadVersion();
  try{
    currentProfile=(await api("/auth/me")).profile;showApp();
    const [pr,m]=await Promise.all([api("/profiles"),api("/reference/manufacturers")]);
    profiles=pr.profiles||[];manufacturers=m.manufacturers||[];renderProfiles();
    await loadSettings();await loadPresets();await newSearch(true);
    await Promise.all([loadResults(),loadMyCars(),loadSold(),loadUnsuitable()]);
    const my=(await api("/my-cars")).cars||[];
    page(my.length?"mycars":"settings");
  }catch{showLogin()}
}
function renderProfiles(){$("profileSelect").innerHTML=profiles.map(p=>`<option value="${p.id}" ${p.id===currentProfile.id?"selected":""}>${esc(p.name)}</option>`).join("")}
$("profileSelect").onchange=async e=>{currentProfile=(await api("/auth/switch",{method:"POST",body:JSON.stringify({profile_id:+e.target.value})})).profile;await boot()};
$("loginForm").onsubmit=async e=>{e.preventDefault();$("loginError").classList.add("hidden");$("loginChoices").classList.add("hidden");const name=$("loginName").value.trim(),r=await api("/auth/name",{method:"POST",body:JSON.stringify({name,create:false})});if(r.status==="ok")return boot();showUnknown(r.entered_name,r.suggestions||[])};
function showUnknown(name,s){$("loginError").textContent=`"${name}" is not an existing name.`;$("loginError").classList.remove("hidden");let h=s.length?`<p class="muted">Did you mean?</p>${s.map(p=>`<button type="button" class="secondary use-name" data-name="${esc(p.name)}">Use ${esc(p.name)}</button>`).join("")}`:"";h+=`<button type="button" class="primary" id="addName">Add "${esc(name)}" as a new user</button>`;$("loginChoices").innerHTML=h;$("loginChoices").classList.remove("hidden");document.querySelectorAll(".use-name").forEach(b=>b.onclick=()=>{$("loginName").value=b.dataset.name;$("loginForm").requestSubmit()});$("addName").onclick=async()=>{if(confirm(`Add "${name}" as a new CarFinder user?`)){await api("/auth/name",{method:"POST",body:JSON.stringify({name,create:true})});await boot()}}}
$("logoutButton").onclick=async()=>{try{await api("/auth/logout",{method:"POST"})}catch{}$("loginName").value="";showLogin()};
document.querySelectorAll(".nav-item").forEach(b=>b.onclick=()=>page(b.dataset.page));

function parseManufacturerModels(spec){
  const out={};
  String(spec||"").split(";").map(x=>x.trim()).filter(Boolean).forEach(part=>{
    const dash=part.indexOf("-");
    const make=(dash<0?part:part.slice(0,dash)).trim();
    const models=(dash<0?"":part.slice(dash+1)).trim();
    if(make)out[make]=models;
  });
  return out;
}
function formatManufacturerModels(map){
  return Object.keys(map).map(make=>{
    const models=String(map[make]||"").trim();
    return models?`${make}-${models}`:make;
  }).join(";");
}
function makerHtml(selected=[]){
  return manufacturers.map(m=>`<label><input type="checkbox" value="${esc(m)}" ${selected.includes(m)?"checked":""}>${esc(m)}</label>`).join("");
}
function selectedMakers(container){return [...document.querySelectorAll(`#${container} input:checked`)].map(x=>x.value)}
function colourHtml(selected=[]){return COLOURS.map(c=>`<label><input type="checkbox" value="${c}" ${selected.includes(c)?"checked":""}>${c}</label>`).join("")}
function selectedColours(container){return [...document.querySelectorAll(`#${container} input:checked`)].map(x=>x.value)}
function renderColours(container,summary,selected=[]){
  $(container).innerHTML=colourHtml(selected);
  const update=()=>{const a=selectedColours(container);$(summary).textContent=a.length?a.join(", "):"Any colour"};
  document.querySelectorAll(`#${container} input`).forEach(x=>x.onchange=update);update();
}

function renderMakerPicker(container,summary,selected,changeHandler){
  $(container).innerHTML=makerHtml(selected);
  document.querySelectorAll(`#${container} input`).forEach(x=>x.onchange=changeHandler);
  $(summary).textContent=selected.length?`${selected.length} selected`:"Choose manufacturers";
}
function syncMakerSelection(target){
  const isPreset=target==="preset",container=isPreset?"presetManufacturerList":"manufacturerList",summary=isPreset?"presetManufacturerSummary":"manufacturerSummary",button=isPreset?"presetModelsButton":"modelsButton",summaryBox=isPreset?"presetManufacturerModelsSummary":"manufacturerModelsSummary",map=isPreset?presetModels:searchModels,selected=selectedMakers(container);
  Object.keys(map).forEach(make=>{if(!selected.includes(make))delete map[make]});
  selected.forEach(make=>{if(!(make in map))map[make]=""});
  $(summary).textContent=selected.length?`${selected.length} selected`:"Choose manufacturers";
  $(button).disabled=!selected.length;$(summaryBox).value=formatManufacturerModels(map);
}
function loadMakerEditor(target,spec){
  const map=parseManufacturerModels(spec);if(target==="preset")presetModels=map;else searchModels=map;
  const selected=Object.keys(map),container=target==="preset"?"presetManufacturerList":"manufacturerList",summary=target==="preset"?"presetManufacturerSummary":"manufacturerSummary";
  renderMakerPicker(container,summary,selected,()=>syncMakerSelection(target));syncMakerSelection(target);
}
function openModelsEditor(target){
  const map=target==="preset"?presetModels:searchModels,makes=Object.keys(map);if(!makes.length)return;
  modelEditorTarget=target;$("modelsRows").innerHTML=makes.map(make=>`<div class="model-table-row"><div class="model-make">${esc(make)}</div><input class="model-entry" data-make="${esc(make)}" value="${esc(map[make]||"")}" placeholder="e.g. Golf,Passat"></div>`).join("");$("modelsDialog").showModal();
}
$("modelsButton").onclick=()=>openModelsEditor("search");$("presetModelsButton").onclick=()=>openModelsEditor("preset");$("closeModelsDialog").onclick=()=>$("modelsDialog").close();$("cancelModelsButton").onclick=()=>$("modelsDialog").close();
$("modelsForm").onsubmit=e=>{e.preventDefault();const map=modelEditorTarget==="preset"?presetModels:searchModels;document.querySelectorAll("#modelsRows .model-entry").forEach(input=>{map[input.dataset.make]=input.value.trim()});syncMakerSelection(modelEditorTarget);$("modelsDialog").close()};

async function loadSettings(){
  settings=await api("/settings");
  for(const [id,k] of [["settingsPostcode","postcode"],["settingsFuel","fuel"],["settingsBody","body_type"],["settingsGearbox","transmission"],["settingsMinPrice","min_price"],["settingsMaxPrice","max_price"],["settingsMaxMileage","max_mileage"],["settingsMinYear","min_year"],["settingsSeats","min_seats"],["settingsRadius","radius_miles"]])val(id,settings[k]);
  renderColours("settingsColourList","settingsColourSummary",settings.colours||[]);
}
function settingsPayload(){return{postcode:$("settingsPostcode").value,fuel:$("settingsFuel").value||null,body_type:$("settingsBody").value||null,transmission:$("settingsGearbox").value||null,colours:selectedColours("settingsColourList"),min_price:num($("settingsMinPrice").value),max_price:num($("settingsMaxPrice").value),max_mileage:num($("settingsMaxMileage").value),min_year:num($("settingsMinYear").value),min_seats:num($("settingsSeats").value),radius_miles:num($("settingsRadius").value)}}
$("settingsForm").onsubmit=async e=>{e.preventDefault();settings=await api("/settings",{method:"PUT",body:JSON.stringify(settingsPayload())});$("settingsSaved").textContent="Settings saved";setTimeout(()=>$("settingsSaved").textContent="",1500)};

async function loadPresets(){
  presets=(await api("/search-presets")).presets||[];
  $("presetEmpty").classList.toggle("hidden",!!presets.length);
  $("presetList").innerHTML=presets.map(p=>`<div class="preset-row"><div><div class="preset-name">${esc(p.name)}</div></div><div class="preset-summary">${esc(p.manufacturer_models||"No manufacturers")} · ${esc((p.colours||[]).join(", ")||"Any colour")} · ${esc(p.body_type||"Any body")} · ${p.min_price!=null?`£${Number(p.min_price).toLocaleString()}+`:"Any price"} · ${p.radius_miles?`${p.radius_miles} miles`:"Nationwide"}</div><div class="preset-actions"><button class="primary" onclick="searchPresetNow(${p.id})">Search Now</button><button class="secondary" onclick="editPreset(${p.id})">Edit</button><button class="danger" onclick="deletePreset(${p.id})">Delete</button></div></div>`).join("");
}
function applySearch(d){
  loadMakerEditor("search",d.manufacturer_models||"");
  for(const [id,k] of [["searchPostcode","postcode"],["searchFuel","fuel"],["searchBody","body_type"],["searchGearbox","transmission"],["searchMinPrice","min_price"],["searchMaxPrice","max_price"],["searchMileage","max_mileage"],["searchYear","min_year"],["searchSeats","seats"],["searchRadius","radius_miles"]])val(id,d[k]);
  renderColours("searchColourList","searchColourSummary",d.colours||[]);
}
async function newSearch(first=false){
  $("searchName").value="";
  applySearch(await api("/search/base"));
  $("searchStatus").classList.add("hidden");
  if(!first){
    page("search");
    $("newSearchDialog").showModal();
    requestAnimationFrame(()=>$("searchName").focus());
  }
}
$("newSearchButton").onclick=()=>newSearch(false);
$("closeNewSearchDialog").onclick=()=>$("newSearchDialog").close();
$("cancelNewSearchButton").onclick=()=>$("newSearchDialog").close();
function searchPayload(){return{manufacturer_models:formatManufacturerModels(searchModels),postcode:$("searchPostcode").value,fuel:$("searchFuel").value||null,body_type:$("searchBody").value||null,transmission:$("searchGearbox").value||null,colours:selectedColours("searchColourList"),min_price:num($("searchMinPrice").value),max_price:num($("searchMaxPrice").value),max_mileage:num($("searchMileage").value),min_year:num($("searchYear").value),seats:num($("searchSeats").value),radius_miles:num($("searchRadius").value)}}
function setSearchBusy(busy,savedSearchName=""){
  searchInProgress=busy;
  $("searchRunning").classList.toggle("hidden",!busy);
  $("presetSearchRunning").classList.toggle("hidden",!busy);
  const popupOpen=$("newSearchDialog").open;
  $("newSearchDialogRunning").classList.toggle("hidden",!(busy&&popupOpen));
  $("searchRunningDetail").textContent=busy?"Starting search…":"";
  $("newSearchDialogRunningDetail").textContent=busy?"Starting search…":"";
  $("presetSearchRunningName").textContent=busy?(savedSearchName?`Running ${savedSearchName} · starting…`:"Starting search…"):"";
  document.querySelectorAll("#page-search input,#page-search select,#page-search button,#page-search details,#newSearchDialog input,#newSearchDialog select,#newSearchDialog button,#newSearchDialog details,#presetList button").forEach(el=>{
    if(el.tagName==="DETAILS")el.classList.toggle("busy-disabled",busy);
    else if(el.id!=="closeNewSearchDialog")el.disabled=busy;
  });
  $("presetList").classList.toggle("search-list-busy",busy);
  $("startSearchButton").textContent=busy?"Searching…":"Search Now";
}
function progressText(job,startedAt){
  const elapsed=Math.max(0,Math.floor((Date.now()-startedAt)/1000));
  if(job.phase==="saving")return `${job.total}/${job.total} · Saving results · ${elapsed}s elapsed`;
  if(job.current>0)return `${job.current}/${job.total} · ${job.label||"Searching"} · ${elapsed}s elapsed`;
  return `0/${job.total||"?"} · Starting · ${elapsed}s elapsed`;
}
async function waitForSearchJob(jobId,savedSearchName,startedAt){
  while(true){
    const job=await api(`/search/status/${jobId}`);
    const detail=progressText(job,startedAt);
    $("searchRunningDetail").textContent=detail;
    $("newSearchDialogRunningDetail").textContent=detail;
    $("presetSearchRunningName").textContent=savedSearchName?`Running ${savedSearchName} · ${detail}`:detail;
    if(job.done){
      if(job.error)throw Error(job.error);
      return job.result;
    }
    await new Promise(resolve=>setTimeout(resolve,900));
  }
}
async function runAsyncSearch(payload,savedSearchName=""){
  if(searchInProgress)return;
  const box=$("searchStatus");box.classList.add("hidden");setSearchBusy(true,savedSearchName);
  const startedAt=Date.now();
  try{
    const started=await api("/search/start-async",{method:"POST",body:JSON.stringify(payload)});
    if(started.status!=="started"){
      box.textContent=started.message||"Search could not be started.";
      box.classList.remove("hidden");
      return;
    }
    const r=await waitForSearchJob(started.job_id,savedSearchName,startedAt);
    box.textContent=r.message+(r.failures?.length?` Problems: ${r.failures.join(" | ")}`:"");
    await Promise.all([loadResults(),loadMyCars(),loadSold(),loadUnsuitable()]);
    if(r.status==="complete"){
      if($("newSearchDialog").open)$("newSearchDialog").close();
      page("results");
    }else box.classList.remove("hidden");
  }catch(e){
    box.textContent=e.message;
    box.classList.remove("hidden");
  }finally{
    setSearchBusy(false);
  }
}
async function runCurrentSearch(){await runAsyncSearch(searchPayload())}
$("startSearchButton").onclick=runCurrentSearch;
$("saveSearchButton").onclick=async()=>{
  const name=$("searchName").value.trim();
  if(!name){alert("Enter a Search name to save it.");return}
  try{
    const q=searchPayload();
    await api("/search-presets",{method:"POST",body:JSON.stringify({
      name,manufacturer_models:q.manufacturer_models,postcode:q.postcode,fuel:q.fuel,body_type:q.body_type,
      transmission:q.transmission,colours:q.colours,min_price:q.min_price,max_price:q.max_price,
      max_mileage:q.max_mileage,min_year:q.min_year,min_seats:q.seats,radius_miles:q.radius_miles
    })});
    await loadPresets();
    if($("newSearchDialog").open)$("newSearchDialog").close();
  }catch(e){alert(e.message)}
};

function openPreset(p){$("presetId").value=p.id;$("presetDialogTitle").textContent="Edit Search";val("presetName",p.name);loadMakerEditor("preset",p.manufacturer_models||"");renderColours("presetColourList","presetColourSummary",p.colours||[]);for(const [id,k] of [["presetPostcode","postcode"],["presetFuel","fuel"],["presetBody","body_type"],["presetGearbox","transmission"],["presetMinPrice","min_price"],["presetMaxPrice","max_price"],["presetMaxMileage","max_mileage"],["presetMinYear","min_year"],["presetSeats","min_seats"],["presetRadius","radius_miles"]])val(id,p[k]);$("presetDialog").showModal()}
window.searchPresetNow=async id=>{
  const p=presets.find(x=>x.id===id);if(!p)return;
  const payload={manufacturer_models:p.manufacturer_models||"",postcode:p.postcode||"",fuel:p.fuel||null,body_type:p.body_type||null,transmission:p.transmission||null,colours:p.colours||[],min_price:p.min_price,max_price:p.max_price,max_mileage:p.max_mileage,min_year:p.min_year,seats:p.min_seats,radius_miles:p.radius_miles};
  await runAsyncSearch(payload,p.name);
};
window.editPreset=id=>openPreset(presets.find(p=>p.id===id));
window.deletePreset=async id=>{if(confirm("Delete this Search?")){await api(`/search-presets/${id}`,{method:"DELETE"});await loadPresets()}};
$("closePresetDialog").onclick=()=>$("presetDialog").close();$("cancelPresetButton").onclick=()=>$("presetDialog").close();
$("presetForm").onsubmit=async e=>{e.preventDefault();const id=+$("presetId").value;const payload={name:$("presetName").value,manufacturer_models:formatManufacturerModels(presetModels),postcode:$("presetPostcode").value,fuel:$("presetFuel").value||null,body_type:$("presetBody").value||null,transmission:$("presetGearbox").value||null,colours:selectedColours("presetColourList"),min_price:num($("presetMinPrice").value),max_price:num($("presetMaxPrice").value),max_mileage:num($("presetMaxMileage").value),min_year:num($("presetMinYear").value),min_seats:num($("presetSeats").value),radius_miles:num($("presetRadius").value)};await api(`/search-presets/${id}`,{method:"PUT",body:JSON.stringify(payload)});$("presetDialog").close();await loadPresets()};

function priceText(v){if(v.price==null)return"Price unknown";const current=`£${Number(v.price).toLocaleString()}`;if(v.in_my_cars&&v.initial_price!=null&&Number(v.price)!==Number(v.initial_price))return `<span class="price-changed">${current}</span> <span class="old-price">(£${Number(v.initial_price).toLocaleString()})</span>`;return current}
function facts(v){return[priceText(v),v.mileage!=null?`${Number(v.mileage).toLocaleString()} miles`:null,v.colour||null,v.distance_miles!=null?`${Math.round(v.distance_miles)} miles away`:null,v.dealer_name||null].filter(Boolean).map(x=>`<span>${x}</span>`).join("")}
function resultRow(v){const state=v.sold?"sold-hit":(v.in_my_cars?"saved-hit":"");let actions="";if(v.sold)actions=`<button class="secondary" onclick="makeAvailable(${v.id})">Available</button>`;else if(!v.in_my_cars)actions=`<button class="primary" onclick="addMyCar(${v.id})">Interested</button><button class="danger" onclick="rejectCar(${v.id})">Not Suitable</button>`;else actions=`<button class="danger" onclick="rejectCar(${v.id})">Not Suitable</button>`;if(v.url)actions+=`<a class="secondary" href="${esc(v.url)}" target="_blank" rel="noopener">Advert</a>`;return `<article class="result-row ${state}"><div class="row-main"><div><div class="car-title">${esc([v.make,v.model,v.trim].filter(Boolean).join(" "))}</div><div class="car-sub">${esc(v.registration)}${v.sold?' · <span class="sold-label">SOLD</span>':(v.in_my_cars?' · <span class="saved-label">IN MY CARS</span>':"")}</div></div><div class="facts">${facts(v)}</div><div class="row-actions">${actions}</div></div></article>`}
function sortValue(v,key){
  if(key==="model")return `${v.make||""} ${v.model||""}`.toLowerCase();
  if(key==="dealer")return String(v.dealer_name||"").toLowerCase();
  if(key==="distance")return v.distance_miles==null?Number.POSITIVE_INFINITY:Number(v.distance_miles);
  if(key==="year")return v.year==null?0:Number(v.year);
  if(key==="mileage")return v.mileage==null?Number.POSITIVE_INFINITY:Number(v.mileage);
  if(key==="price")return v.price==null?Number.POSITIVE_INFINITY:Number(v.price);
  return "";
}
function compareBy(x,y,key,asc){
  const ax=sortValue(x,key),ay=sortValue(y,key);
  let c=typeof ax==="string"?ax.localeCompare(ay):ax-ay;
  return asc?c:-c;
}
function renderResults(){
  let a=[...resultRows];
  if(resultView==="unreviewed")a=a.filter(v=>!v.in_my_cars);
  if(resultView==="mycars")a=a.filter(v=>v.in_my_cars);
  a.sort((x,y)=>{
    for(const s of resultSorts){
      if(!s.key)continue;
      const c=compareBy(x,y,s.key,s.asc);
      if(c)return c;
    }
    return 0;
  });
  const unreviewed=resultRows.filter(v=>!v.in_my_cars).length,my=resultRows.filter(v=>v.in_my_cars).length;
  $("resultCounts").innerHTML=`<span>${resultRows.length} results</span><span>${unreviewed} unreviewed</span><span>${my} My Cars</span>`;
  $("resultsEmpty").classList.toggle("hidden",!!a.length);
  $("resultList").innerHTML=a.map(resultRow).join("");
  for(const [id,view] of [["viewAll","all"],["viewUnreviewed","unreviewed"],["viewMyCars","mycars"]])$(id).className=resultView===view?"primary":"secondary";
}
async function loadResults(){resultRows=(await api("/results")).results||[];renderResults()}
$("viewAll").onclick=()=>{resultView="all";renderResults()};
$("viewUnreviewed").onclick=()=>{resultView="unreviewed";renderResults()};
$("viewMyCars").onclick=()=>{resultView="mycars";renderResults()};
for(let i=1;i<=3;i++){
  $(`resultSort${i}`).onchange=e=>{resultSorts[i-1].key=e.target.value;renderResults()};
  $(`sortDirection${i}`).onclick=()=>{
    resultSorts[i-1].asc=!resultSorts[i-1].asc;
    $(`sortDirection${i}`).textContent=resultSorts[i-1].asc?"↑":"↓";
    $(`sortDirection${i}`).title=resultSorts[i-1].asc?"Ascending":"Descending";
    renderResults();
  };
}
window.addMyCar=async id=>{await api(`/my-cars/${id}`,{method:"POST"});await Promise.all([loadResults(),loadMyCars(),loadSold()])};window.rejectCar=async id=>{await api(`/unsuitable/${id}`,{method:"POST"});await Promise.all([loadResults(),loadMyCars(),loadSold(),loadUnsuitable()])};window.makeAvailable=async id=>{await api(`/my-cars/${id}/available`,{method:"POST"});await Promise.all([loadResults(),loadMyCars(),loadSold()])};window.markSold=async id=>{await api(`/my-cars/${id}/sold`,{method:"POST"});await Promise.all([loadResults(),loadMyCars(),loadSold()])};

function myCarRow(v,fromSold=false){const changed=v.initial_price!=null&&v.price!=null&&Number(v.price)!==Number(v.initial_price);const price=v.price==null?"Price unknown":changed?`<span class="price-changed">£${Number(v.price).toLocaleString()}</span> <span class="old-price">(£${Number(v.initial_price).toLocaleString()})</span>`:`£${Number(v.price).toLocaleString()}`;const state=v.sold?"sold-purple":"mycar-green";let actions=v.url?`<a class="secondary" href="${esc(v.url)}" target="_blank" rel="noopener">Advert</a>`:"";actions+=v.sold?`<button class="secondary" onclick="makeAvailable(${v.id})">Available</button>`:`<button class="secondary" onclick="markSold(${v.id})">Sold</button>`;if(!fromSold)actions+=`<button class="danger" onclick="removeMyCar(${v.id})">Remove</button>`;return `<article class="saved-row ${state}"><div class="row-main"><div><div class="car-title">${esc([v.make,v.model,v.trim].filter(Boolean).join(" "))}</div><div class="car-sub">${esc(v.registration)}${v.year?` · ${v.year}`:""} · Added by ${esc(v.added_by||"family")}${v.sold?' · SOLD':""}</div></div><div class="facts">${[price,v.mileage!=null?`${Number(v.mileage).toLocaleString()} miles`:null,v.colour||null,v.distance_miles!=null?`${Math.round(v.distance_miles)} miles away`:null,v.dealer_name||null].filter(Boolean).map(x=>`<span>${x}</span>`).join("")}</div><div class="row-actions">${actions}</div></div>${fromSold?"":`<div class="notes-block"><textarea id="my-note-${v.id}" placeholder="Family notes">${esc(v.notes||"")}</textarea><button class="secondary" onclick="saveNote(${v.id})">Save note</button></div>`}</article>`}
async function loadMyCars(){const a=(await api("/my-cars")).cars||[];$("myCarsEmpty").classList.toggle("hidden",!!a.length);$("myCarsList").innerHTML=a.map(v=>myCarRow(v,false)).join("")}
async function loadSold(){const a=(await api("/sold")).cars||[];$("soldEmpty").classList.toggle("hidden",!!a.length);$("soldList").innerHTML=a.map(v=>myCarRow(v,true)).join("")}
window.removeMyCar=async id=>{if(confirm("Remove this car from My Cars?")){await api(`/my-cars/${id}`,{method:"DELETE"});await Promise.all([loadMyCars(),loadSold(),loadResults()])}};window.saveNote=async id=>{await api(`/my-cars/${id}/notes`,{method:"PUT",body:JSON.stringify({notes:$(`my-note-${id}`).value})})};

async function loadUnsuitable(){const a=(await api("/unsuitable")).cars||[];$("unsuitableEmpty").classList.toggle("hidden",!!a.length);$("unsuitableList").innerHTML=a.map(x=>`<article class="simple-row unsuitable-red"><div class="row-main"><div><div class="car-title">${esc([x.make,x.model,x.trim].filter(Boolean).join(" ")||x.registration)}</div><div class="car-sub">${esc(x.registration)} · Marked unsuitable${x.rejected_by?` by ${esc(x.rejected_by)}`:""}</div></div><div class="facts">${[x.price!=null?`£${Number(x.price).toLocaleString()}`:null,x.mileage!=null?`${Number(x.mileage).toLocaleString()} miles`:null,x.colour||null,x.distance_miles!=null?`${Math.round(x.distance_miles)} miles away`:null,x.dealer_name||null].filter(Boolean).map(v=>`<span>${v}</span>`).join("")}</div><div class="row-actions">${x.url?`<a class="secondary" href="${esc(x.url)}" target="_blank" rel="noopener">Advert</a>`:""}<button class="secondary" onclick="restoreUnsuitable('${esc(x.registration)}')">Allow again</button></div></div></article>`).join("")}
window.restoreUnsuitable=async reg=>{await api(`/unsuitable/${encodeURIComponent(reg)}`,{method:"DELETE"});await loadUnsuitable()};
boot();
