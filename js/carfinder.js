const API="/api/v1",$=id=>document.getElementById(id);let profiles=[],currentProfile=null,presets=[],settings={};
async function api(path,options={}){const r=await fetch(API+path,{credentials:"same-origin",headers:{"Content-Type":"application/json"},...options});let body={};try{body=await r.json()}catch{}if(r.status===401){showLogin();throw Error(body.detail||"Choose your name")}if(!r.ok)throw Error(body.detail||`Request failed (${r.status})`);return body}
async function loadVersion(){try{const r=await fetch("/build-info.json",{cache:"no-store"});if(r.ok){const b=await r.json(),v=b.version||b.tag;if(v)$("versionButton").textContent=String(v).startsWith("v")?v:`v${v}`}}catch{}}
function showLogin(){$("loginView").classList.remove("hidden");$("appView").classList.add("hidden")}function showApp(){$("loginView").classList.add("hidden");$("appView").classList.remove("hidden")}function page(name){document.querySelectorAll(".page").forEach(x=>x.classList.toggle("active",x.id===`page-${name}`));document.querySelectorAll(".nav-item").forEach(x=>x.classList.toggle("active",x.dataset.page===name));scrollTo(0,0)}
const num=v=>v===""||v==null?null:Number(v),val=(id,v)=>$(id).value=v??"",esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));

async function boot(){
  loadVersion();
  try{
    currentProfile=(await api("/auth/me")).profile;showApp();
    const pr=await api("/profiles");
    profiles=pr.profiles||[];renderProfiles();
    await loadSettings();await loadPresets();await newSearch(true);
    await Promise.all([loadResults(),loadMyCars(),loadUnsuitable()]);
  }catch{showLogin()}
}
function renderProfiles(){$("profileSelect").innerHTML=profiles.map(p=>`<option value="${p.id}" ${p.id===currentProfile.id?"selected":""}>${esc(p.name)}</option>`).join("")}
$("profileSelect").onchange=async e=>{currentProfile=(await api("/auth/switch",{method:"POST",body:JSON.stringify({profile_id:+e.target.value})})).profile;await boot()};
$("loginForm").onsubmit=async e=>{e.preventDefault();$("loginError").classList.add("hidden");$("loginChoices").classList.add("hidden");const name=$("loginName").value.trim(),r=await api("/auth/name",{method:"POST",body:JSON.stringify({name,create:false})});if(r.status==="ok")return boot();showUnknown(r.entered_name,r.suggestions||[])};
function showUnknown(name,s){$("loginError").textContent=`"${name}" is not an existing name.`;$("loginError").classList.remove("hidden");let h=s.length?`<p class="muted">Did you mean?</p>${s.map(p=>`<button type="button" class="secondary use-name" data-name="${esc(p.name)}">Use ${esc(p.name)}</button>`).join("")}`:"";h+=`<button type="button" class="primary" id="addName">Add "${esc(name)}" as a new user</button>`;$("loginChoices").innerHTML=h;$("loginChoices").classList.remove("hidden");document.querySelectorAll(".use-name").forEach(b=>b.onclick=()=>{$("loginName").value=b.dataset.name;$("loginForm").requestSubmit()});$("addName").onclick=async()=>{if(confirm(`Add "${name}" as a new CarFinder user?`)){await api("/auth/name",{method:"POST",body:JSON.stringify({name,create:true})});await boot()}}}
$("logoutButton").onclick=async()=>{try{await api("/auth/logout",{method:"POST"})}catch{}$("loginName").value="";showLogin()};
document.querySelectorAll(".nav-item").forEach(b=>b.onclick=()=>page(b.dataset.page));

async function loadSettings(){
  settings=await api("/settings");
  for(const [id,k] of [["settingsPostcode","postcode"],["settingsFuel","fuel"],["settingsBody","body_type"],["settingsGearbox","transmission"],["settingsMinPrice","min_price"],["settingsMaxPrice","max_price"],["settingsMaxMileage","max_mileage"],["settingsMinYear","min_year"],["settingsSeats","min_seats"],["settingsPower","min_power_bhp"],["settingsRadius","radius_miles"]])val(id,settings[k]);
}
function settingsPayload(){return{postcode:$("settingsPostcode").value,fuel:$("settingsFuel").value||null,body_type:$("settingsBody").value||null,transmission:$("settingsGearbox").value||null,min_price:num($("settingsMinPrice").value),max_price:num($("settingsMaxPrice").value),max_mileage:num($("settingsMaxMileage").value),min_year:num($("settingsMinYear").value),min_seats:num($("settingsSeats").value),min_power_bhp:num($("settingsPower").value),radius_miles:num($("settingsRadius").value)||50}}
$("settingsForm").onsubmit=async e=>{e.preventDefault();settings=await api("/settings",{method:"PUT",body:JSON.stringify(settingsPayload())});$("settingsSaved").textContent="Defaults saved";setTimeout(()=>$("settingsSaved").textContent="",1500)};

async function loadPresets(){
  presets=(await api("/search-presets")).presets||[];
  $("searchPresetSelect").innerHTML=`<option value="">Settings defaults</option>`+presets.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("presetEmpty").classList.toggle("hidden",!!presets.length);
  $("presetList").innerHTML=presets.map(p=>`<div class="preset-row"><div><div class="preset-name">${esc(p.name)}</div></div><div class="preset-summary">${esc(p.manufacturer_models||"No manufacturers/models")} · ${esc(p.body_type||"Any body")} · ${p.min_price!=null?`£${Number(p.min_price).toLocaleString()}+`:"Any price"} · ${p.radius_miles||50} miles</div><div class="preset-actions"><button class="secondary" onclick="editPreset(${p.id})">Edit</button><button class="danger" onclick="deletePreset(${p.id})">Delete</button></div></div>`).join("");
}
function searchFromPresetId(id){return id?presets.find(p=>p.id===Number(id)):null}
function applySearch(d){
  val("manufacturerModels",d.manufacturer_models||"");
  for(const [id,k] of [["searchPostcode","postcode"],["searchFuel","fuel"],["searchBody","body_type"],["searchGearbox","transmission"],["searchMinPrice","min_price"],["searchMaxPrice","max_price"],["searchMileage","max_mileage"],["searchYear","min_year"],["searchSeats","seats"],["searchPower","min_power_bhp"],["searchRadius","radius_miles"]])val(id,d[k]);
}
async function newSearch(first=false){
  const preset=searchFromPresetId($("searchPresetSelect").value);
  if(preset)applySearch({...preset,seats:preset.min_seats});
  else applySearch(await api("/search/base"));
  if(!first){$("searchStatus").classList.add("hidden");page("search")}
}
$("newSearchButton").onclick=()=>newSearch(false);
function searchPayload(){return{manufacturer_models:$("manufacturerModels").value.trim(),postcode:$("searchPostcode").value,fuel:$("searchFuel").value||null,body_type:$("searchBody").value||null,transmission:$("searchGearbox").value||null,min_price:num($("searchMinPrice").value),max_price:num($("searchMaxPrice").value),max_mileage:num($("searchMileage").value),min_year:num($("searchYear").value),seats:num($("searchSeats").value),min_power_bhp:num($("searchPower").value),radius_miles:num($("searchRadius").value)||50}}
$("startSearchButton").onclick=async()=>{const box=$("searchStatus"),btn=$("startSearchButton");box.classList.remove("hidden");box.textContent="Searching live manufacturer stock…";btn.disabled=true;try{const r=await api("/search/start",{method:"POST",body:JSON.stringify(searchPayload())});box.textContent=r.message+(r.failures?.length?` Problems: ${r.failures.join(" | ")}`:"");await Promise.all([loadResults(),loadMyCars()]);if(r.status==="complete")page("results")}catch(e){box.textContent=e.message}finally{btn.disabled=false}};

$("addPresetButton").onclick=async()=>{const name=prompt("Name for the new Search:");if(!name)return;const p=await api("/search-presets",{method:"POST",body:JSON.stringify({name})});await loadPresets();openPreset(p)};
function openPreset(p){
  $("presetId").value=p.id;$("presetDialogTitle").textContent="Edit Search";val("presetName",p.name);val("presetManufacturerModels",p.manufacturer_models||"");
  for(const [id,k] of [["presetPostcode","postcode"],["presetFuel","fuel"],["presetBody","body_type"],["presetGearbox","transmission"],["presetMinPrice","min_price"],["presetMaxPrice","max_price"],["presetMaxMileage","max_mileage"],["presetMinYear","min_year"],["presetSeats","min_seats"],["presetPower","min_power_bhp"],["presetRadius","radius_miles"]])val(id,p[k]);
  $("presetDialog").showModal();
}
window.editPreset=id=>openPreset(presets.find(p=>p.id===id));
window.deletePreset=async id=>{if(confirm("Delete this Search?")){await api(`/search-presets/${id}`,{method:"DELETE"});await loadPresets()}};
$("closePresetDialog").onclick=()=>$("presetDialog").close();$("cancelPresetButton").onclick=()=>$("presetDialog").close();
$("presetForm").onsubmit=async e=>{e.preventDefault();const id=+$("presetId").value;const payload={name:$("presetName").value,manufacturer_models:$("presetManufacturerModels").value.trim(),postcode:$("presetPostcode").value,fuel:$("presetFuel").value||null,body_type:$("presetBody").value||null,transmission:$("presetGearbox").value||null,min_price:num($("presetMinPrice").value),max_price:num($("presetMaxPrice").value),max_mileage:num($("presetMaxMileage").value),min_year:num($("presetMinYear").value),min_seats:num($("presetSeats").value),min_power_bhp:num($("presetPower").value),radius_miles:num($("presetRadius").value)||50};await api(`/search-presets/${id}`,{method:"PUT",body:JSON.stringify(payload)});$("presetDialog").close();await loadPresets()};

function priceText(v){if(v.price==null)return"Price unknown";let s=`£${Number(v.price).toLocaleString()}`;if(v.in_my_cars&&v.initial_price!=null&&v.price_change!=null&&v.price_change!==0){const d=Math.abs(v.price_change).toLocaleString();s+=v.price_change<0?` <span class="price-down">↓ £${d}</span>`:` <span class="price-up">↑ £${d}</span>`}return s}
function facts(v){return[priceText(v),v.mileage!=null?`${Number(v.mileage).toLocaleString()} miles`:null,v.distance_miles!=null?`${Math.round(v.distance_miles)} miles away`:null,v.dealer_name||null].filter(Boolean).map(x=>`<span>${x}</span>`).join("")}
function resultRow(v){return`<article class="result-row ${v.in_my_cars?"saved-hit":""}"><div class="row-main"><div><div class="car-title">${esc([v.make,v.model,v.trim].filter(Boolean).join(" "))}</div><div class="car-sub">${esc(v.registration)}${v.in_my_cars?' · <span class="saved-label">IN MY CARS</span>':""}</div></div><div class="facts">${facts(v)}</div><div class="row-actions">${v.in_my_cars?"":`<button class="primary" onclick="addMyCar(${v.id})">Interested</button>`}<button class="danger" onclick="rejectCar(${v.id})">Not Suitable</button>${v.url?`<a class="secondary" href="${esc(v.url)}" target="_blank" rel="noopener">Advert</a>`:""}</div></div></article>`}
async function loadResults(){const a=(await api("/results")).results||[];$("resultsEmpty").classList.toggle("hidden",!!a.length);$("resultList").innerHTML=a.map(resultRow).join("")}
window.addMyCar=async id=>{await api(`/my-cars/${id}`,{method:"POST"});await Promise.all([loadResults(),loadMyCars()])};window.rejectCar=async id=>{await api(`/unsuitable/${id}`,{method:"POST"});await Promise.all([loadResults(),loadMyCars(),loadUnsuitable()])};

function myCarRow(v){let change="";if(v.initial_price!=null&&v.price!=null){const d=v.price-v.initial_price;if(d<0)change=`<span class="price-down">Dropped £${Math.abs(d).toLocaleString()} from £${Number(v.initial_price).toLocaleString()}</span>`;else if(d>0)change=`<span class="price-up">Up £${d.toLocaleString()} from £${Number(v.initial_price).toLocaleString()}</span>`;else change=`No price change from £${Number(v.initial_price).toLocaleString()}`}return`<article class="saved-row"><div class="row-main"><div><div class="car-title">${esc([v.make,v.model,v.trim].filter(Boolean).join(" "))}</div><div class="car-sub">${esc(v.registration)} · Added by ${esc(v.added_by||"family")}</div></div><div class="facts">${[v.price!=null?`£${Number(v.price).toLocaleString()}`:null,v.mileage!=null?`${Number(v.mileage).toLocaleString()} miles`:null,v.dealer_name||null,change].filter(Boolean).map(x=>`<span>${x}</span>`).join("")}</div><div class="row-actions">${v.url?`<a class="secondary" href="${esc(v.url)}" target="_blank" rel="noopener">Advert</a>`:""}<button class="danger" onclick="removeMyCar(${v.id})">Remove</button></div></div><div class="notes-block"><textarea id="my-note-${v.id}" placeholder="Family notes">${esc(v.notes||"")}</textarea><button class="secondary" onclick="saveNote(${v.id})">Save note</button></div></article>`}
async function loadMyCars(){const a=(await api("/my-cars")).cars||[];$("myCarsEmpty").classList.toggle("hidden",!!a.length);$("myCarsList").innerHTML=a.map(myCarRow).join("")}
window.removeMyCar=async id=>{if(confirm("Remove this car from My Cars?")){await api(`/my-cars/${id}`,{method:"DELETE"});await Promise.all([loadMyCars(),loadResults()])}};window.saveNote=async id=>{await api(`/my-cars/${id}/notes`,{method:"PUT",body:JSON.stringify({notes:$(`my-note-${id}`).value})})};

async function loadUnsuitable(){const a=(await api("/unsuitable")).cars||[];$("unsuitableEmpty").classList.toggle("hidden",!!a.length);$("unsuitableList").innerHTML=a.map(x=>`<article class="simple-row"><div class="row-main"><div><div class="car-title">${esc(x.registration)}</div><div class="car-sub">Marked unsuitable${x.rejected_by?` by ${esc(x.rejected_by)}`:""}</div></div><div></div><div class="row-actions"><button class="secondary" onclick="restoreUnsuitable('${esc(x.registration)}')">Allow again</button></div></div></article>`).join("")}
window.restoreUnsuitable=async reg=>{await api(`/unsuitable/${encodeURIComponent(reg)}`,{method:"DELETE"});await loadUnsuitable()};
boot();
