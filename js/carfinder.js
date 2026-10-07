const API="/api/v1";
const $=id=>document.getElementById(id);
let manufacturers=[];
let currentUser=null;

async function api(path, options={}){
  const opts={credentials:"same-origin",headers:{"Content-Type":"application/json"},...options};
  const response=await fetch(API+path,opts);
  if(response.status===401){ showLogin(); throw new Error("Not authenticated"); }
  let body={};
  try{body=await response.json();}catch{}
  if(!response.ok) throw new Error(body.detail||`Request failed (${response.status})`);
  return body;
}
function showLogin(){ $("loginView").classList.remove("hidden"); $("appView").classList.add("hidden"); }
function showApp(){ $("loginView").classList.add("hidden"); $("appView").classList.remove("hidden"); }
function page(name){
  document.querySelectorAll(".page").forEach(x=>x.classList.toggle("active",x.id===`page-${name}`));
  document.querySelectorAll(".nav-item").forEach(x=>x.classList.toggle("active",x.dataset.page===name));
  window.scrollTo({top:0,behavior:"smooth"});
}
function n(v){ return v===""||v==null?null:Number(v); }
function val(id,v){ $(id).value=v??""; }
function tickedManufacturers(){ return [...document.querySelectorAll("#manufacturerList input:checked")].map(x=>x.value); }

async function boot(){
  try{
    const me=await api("/auth/me");
    currentUser=me.user;
    $("userName").textContent=currentUser.display_name||currentUser.username;
    showApp();
    const refs=await api("/reference/manufacturers");
    manufacturers=refs.manufacturers||[];
    renderManufacturers();
    fillTargetMakes();
    await Promise.all([loadSettings(),loadDiscovery(),loadTargets(),loadResults(),loadInterested()]);
  }catch(e){ showLogin(); }
}
$("loginForm").addEventListener("submit",async e=>{
  e.preventDefault(); $("loginError").classList.add("hidden");
  try{
    await api("/auth/login",{method:"POST",body:JSON.stringify({username:$("loginUsername").value,password:$("loginPassword").value})});
    $("loginPassword").value=""; await boot();
  }catch(err){ $("loginError").textContent=err.message; $("loginError").classList.remove("hidden"); }
});
$("logoutButton").addEventListener("click",async()=>{ try{await api("/auth/logout",{method:"POST"});}catch{} showLogin(); });
document.querySelectorAll(".nav-item").forEach(b=>b.addEventListener("click",()=>page(b.dataset.page)));
document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>page(b.dataset.go)));

function renderManufacturers(selected=[]){
  $("manufacturerList").innerHTML=manufacturers.map(m=>`<label><input type="checkbox" value="${escapeHtml(m)}" ${selected.includes(m)?"checked":""}>${escapeHtml(m)}</label>`).join("");
}
function fillTargetMakes(){ $("targetMake").innerHTML=manufacturers.map(m=>`<option>${escapeHtml(m)}</option>`).join(""); }

async function loadSettings(){
  const s=await api("/settings");
  val("settingsPostcode",s.postcode); val("settingsRadius",s.radius_miles);
  val("settingsMaxPrice",s.max_price); val("settingsMaxMileage",s.max_mileage);
  val("settingsMinYear",s.min_year); val("settingsSeats",s.min_seats); val("settingsPower",s.min_power_bhp);
  if(!$("discoveryRadius").value) val("discoveryRadius",s.radius_miles);
}
$("settingsForm").addEventListener("submit",async e=>{
  e.preventDefault();
  const payload={postcode:$("settingsPostcode").value,radius_miles:n($("settingsRadius").value)||50,
    max_price:n($("settingsMaxPrice").value),max_mileage:n($("settingsMaxMileage").value),
    min_year:n($("settingsMinYear").value),min_seats:n($("settingsSeats").value),min_power_bhp:n($("settingsPower").value)};
  await api("/settings",{method:"PUT",body:JSON.stringify(payload)});
  $("settingsSaved").textContent="Saved"; setTimeout(()=>$("settingsSaved").textContent="",1800);
});

async function loadDiscovery(){
  const d=await api("/discovery");
  renderManufacturers(d.manufacturers||[]);
  val("discoveryFuel",d.fuel); val("discoveryBody",d.body_type); val("discoveryGearbox",d.transmission);
  val("discoveryMinPrice",d.min_price); val("discoveryMaxPrice",d.max_price); val("discoveryMileage",d.max_mileage);
  val("discoveryYear",d.min_year); val("discoverySeats",d.seats); val("discoveryPower",d.min_power_bhp);
  if(d.radius_miles) val("discoveryRadius",d.radius_miles);
}
function discoveryPayload(){
  return {manufacturers:tickedManufacturers(),fuel:$("discoveryFuel").value||null,body_type:$("discoveryBody").value||null,
    transmission:$("discoveryGearbox").value||null,min_price:n($("discoveryMinPrice").value),
    max_price:n($("discoveryMaxPrice").value),max_mileage:n($("discoveryMileage").value),
    min_year:n($("discoveryYear").value),seats:n($("discoverySeats").value),
    min_power_bhp:n($("discoveryPower").value),radius_miles:n($("discoveryRadius").value)};
}
$("discoveryForm").addEventListener("submit",async e=>{
  e.preventDefault(); await api("/discovery",{method:"PUT",body:JSON.stringify(discoveryPayload())});
  $("discoverySaved").textContent="Saved"; setTimeout(()=>$("discoverySaved").textContent="",1800);
});
$("startSearchButton").addEventListener("click",async()=>{
  const box=$("searchStatus"); box.classList.remove("hidden"); box.textContent="Preparing search…";
  try{
    await api("/discovery",{method:"PUT",body:JSON.stringify(discoveryPayload())});
    const r=await api("/search/start",{method:"POST"});
    box.textContent=r.message; if(r.status==="ready") { await loadResults(); page("results"); }
  }catch(err){box.textContent=err.message;}
});

async function loadTargets(){
  const r=await api("/targets"), rows=r.targets||[];
  $("targetList").innerHTML=rows.map(t=>`<article class="target-card">
    <div class="eyebrow">Target ${t.id}</div><h3>${escapeHtml(t.make)} ${escapeHtml(t.model)}</h3>
    <div class="meta-row">${t.body_type?`<span class="pill">${escapeHtml(t.body_type)}</span>`:""}<span class="pill">${t.enabled?"Active":"Paused"}</span></div>
    <p class="muted">${overrideText(t.overrides)}</p>
    <div class="card-actions"><button class="secondary" onclick="deleteTarget(${t.id})">Delete</button></div>
  </article>`).join("");
  $("targetsEmpty").classList.toggle("hidden",rows.length>0);
  $("clearTargetsButton").classList.toggle("hidden",rows.length===0);
}
function overrideText(o={}){
  const bits=[]; if(o.max_price)bits.push(`Max £${Number(o.max_price).toLocaleString()}`);
  if(o.max_mileage)bits.push(`Max ${Number(o.max_mileage).toLocaleString()} miles`);
  if(o.min_year)bits.push(`From ${o.min_year}`);
  return bits.length?`Overrides: ${bits.join(" · ")}`:"Using common limits";
}
window.deleteTarget=async id=>{ if(confirm("Delete this target vehicle?")){await api(`/targets/${id}`,{method:"DELETE"});await loadTargets();}};
$("clearTargetsButton").addEventListener("click",async()=>{if(confirm("Clear ALL target vehicles?")){await api("/targets",{method:"DELETE"});await loadTargets();}});
function openTarget(){ $("targetForm").reset(); $("targetDialog").showModal(); }
$("addTargetButton").addEventListener("click",openTarget); $("emptyAddTargetButton").addEventListener("click",openTarget);
$("cancelTargetButton").addEventListener("click",()=>$("targetDialog").close()); $("closeTargetDialog").addEventListener("click",()=>$("targetDialog").close());
$("targetForm").addEventListener("submit",async e=>{
  e.preventDefault();
  const overrides={}; [["max_price","targetMaxPrice"],["max_mileage","targetMaxMileage"],["min_year","targetMinYear"]].forEach(([k,id])=>{const v=n($(id).value);if(v!=null)overrides[k]=v;});
  await api("/targets",{method:"POST",body:JSON.stringify({make:$("targetMake").value,model:$("targetModel").value,body_type:$("targetBody").value||null,overrides})});
  $("targetDialog").close(); await loadTargets();
});

async function loadResults(){
  const r=await api("/results"),rows=r.results||[]; $("resultsEmpty").classList.toggle("hidden",rows.length>0);
  $("resultList").innerHTML=rows.map(vehicleCard).join("");
}
async function loadInterested(){
  const r=await api("/interested"),rows=r.results||[]; $("interestedEmpty").classList.toggle("hidden",rows.length>0);
  $("interestedList").innerHTML=rows.map(vehicleCard).join("");
}
function vehicleCard(v){
  return `<article class="vehicle-card"><div class="eyebrow">${escapeHtml(v.source||"Vehicle")}</div>
    <h3>${escapeHtml([v.make,v.model,v.trim].filter(Boolean).join(" "))}</h3>
    <div class="meta-row">${v.price?`<span class="pill">£${Number(v.price).toLocaleString()}</span>`:""}${v.mileage?`<span class="pill">${Number(v.mileage).toLocaleString()} miles</span>`:""}${v.distance_miles!=null?`<span class="pill">${v.distance_miles} miles away</span>`:""}</div>
    <p class="muted">${escapeHtml(v.dealer_name||"Dealer not recorded")}</p></article>`;
}
function escapeHtml(x){return String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));}

boot();
