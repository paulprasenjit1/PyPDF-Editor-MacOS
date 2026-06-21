import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const py=fs.readFileSync("pdf_editor_app.py","utf8");
const html=py.match(/INDEX_HTML = r"""\n?([\s\S]*?)\n"""/)[1];
const jsErrors=[]; const vc=new VirtualConsole();
vc.on("jsdomError",e=>jsErrors.push(String(e.detail&&e.detail.message||e.message||e)));
const state={ok:true,open:true,epoch:1,pages:3,filename:"t.pdf",path:"",size_kb:10,
  sizes:[[600,800],[600,800],[600,800]],can_undo:false,dirty:false,rotations:[0,0,0]};
let findQ=null;
const dom=new JSDOM(html,{runScripts:"dangerously",url:"http://127.0.0.1:8123/",pretendToBeVisual:true,virtualConsole:vc,
  beforeParse(window){
    window.IntersectionObserver=class{constructor(cb){this.cb=cb;}observe(t){setTimeout(()=>this.cb([{target:t,isIntersecting:true}]),0);}unobserve(){}disconnect(){}};
    window.URL.createObjectURL=()=>"blob:x"; window.URL.revokeObjectURL=()=>{};
    const jsonRes=o=>({ok:true,headers:{get:h=>h==="Content-Type"?"application/json":null},json:async()=>o,blob:async()=>({size:3})});
    window.fetch=async(u)=>{u=String(u);
      if(u.includes("/api/ping"))return jsonRes({ok:true,epoch:state.epoch});
      if(u.includes("/api/state"))return jsonRes(state);
      if(u.includes("/api/spans"))return jsonRes({ok:true,spans:[]});
      if(u.includes("/api/page"))return{ok:true,headers:{get:h=>h==="Content-Type"?"image/jpeg":null},blob:async()=>({size:3})};
      if(u.includes("/api/find")){findQ=decodeURIComponent(u.split("q=")[1]||"");return jsonRes({ok:true,matches:[{page:0,rect:[10,20,40,30]},{page:1,rect:[10,20,40,30]}]});}
      return jsonRes({ok:true,pages:state.pages});};
  }});
const w=dom.window, doc=dom.window.document;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const fails=[]; const check=(n,c,e="")=>{console.log((c?"PASS ":"FAIL ")+n+(c?"":" — "+e));if(!c)fails.push(n);};
(async()=>{
  await sleep(250);
  const v=doc.getElementById("viewer"); v.scrollTo=()=>{};
  doc.querySelectorAll(".stage").forEach(s=>{s.scrollIntoView=()=>{};});
  check("boot ok",jsErrors.length===0,jsErrors[0]);
  check("findPill present",!!doc.getElementById("findPill"));
  check("navPill present",!!doc.getElementById("navPill"));
  check("findPill not disabled (doc open)",!doc.getElementById("findPill").classList.contains("dis"));
  const fb=doc.getElementById("findBox");
  fb.value="nanda"; fb.dispatchEvent(new w.Event("input",{bubbles:true}));
  await sleep(10);
  check("× shows when typing",doc.getElementById("findClear").style.display==="");
  fb.dispatchEvent(new w.KeyboardEvent("keydown",{key:"Enter",bubbles:true}));
  await sleep(40);
  check("find queried server",findQ==="nanda",findQ);
  check("count shows 1/2",doc.getElementById("findCount").textContent==="1/2",doc.getElementById("findCount").textContent);
  check("next/prev enabled (2 hits)",!doc.getElementById("findNext").disabled);
  // clear via × button
  doc.getElementById("findClear").dispatchEvent(new w.MouseEvent("click",{bubbles:true}));
  await sleep(10);
  check("clear empties field",fb.value==="");
  check("× hidden after clear",doc.getElementById("findClear").style.display==="none");
  // page input still works
  check("pageInput in navpill",doc.getElementById("pageInput").classList.contains("pagebox2"));
  check("no JS errors",jsErrors.length===0,jsErrors[0]);
  console.log("\n"+fails.length+" failed");
  try{ dom.window.close(); }catch(e){}
  process.exit(fails.length?1:0);
})();
