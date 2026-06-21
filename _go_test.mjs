import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const py=fs.readFileSync("pdf_editor_app.py","utf8");
const html=py.match(/INDEX_HTML = r"""\n?([\s\S]*?)\n"""/)[1];
const jsErrors=[]; const vc=new VirtualConsole();
vc.on("jsdomError",e=>jsErrors.push(String(e.detail&&e.detail.message||e.message||e)));
const state={ok:true,open:true,epoch:1,pages:5,filename:"t.pdf",path:"",size_kb:10,
  sizes:[[600,800],[600,800],[600,800],[600,800],[600,800]],can_undo:false,dirty:false,rotations:[0,0,0,0,0]};
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
      return jsonRes({ok:true,pages:state.pages});};
  }});
const w=dom.window, doc=dom.window.document;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const fails=[]; const check=(n,c,e="")=>{console.log((c?"PASS ":"FAIL ")+n+(c?"":" — "+e));if(!c)fails.push(n);};
let scrolled=[];
(async()=>{
  await sleep(250);
  doc.querySelectorAll(".stage").forEach(s=>{ s.scrollIntoView=()=>scrolled.push(+s.dataset.page); });
  check("boot ok",jsErrors.length===0,jsErrors[0]);
  check("5 stages",doc.querySelectorAll(".stage").length===5);
  const pi=doc.getElementById("pageInput"), go=doc.getElementById("goBtn");
  check("goBtn enabled",!go.disabled,"disabled="+go.disabled);
  pi.value="4";
  go.dispatchEvent(new w.MouseEvent("click",{bubbles:true}));
  await sleep(20);
  check("Go button scrolls to page 4 (idx3)", scrolled.includes(3), "scrolled="+JSON.stringify(scrolled));
  scrolled=[]; pi.value="2"; pi.focus();
  pi.dispatchEvent(new w.KeyboardEvent("keydown",{key:"Enter",bubbles:true}));
  await sleep(20);
  check("Enter scrolls to page 2 (idx1)", scrolled.includes(1), "scrolled="+JSON.stringify(scrolled));
  console.log("\n"+fails.length+" failed");
  try{ dom.window.close(); }catch(e){}
  process.exit(0);
})();
