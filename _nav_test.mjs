import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const py=fs.readFileSync("pdf_editor_app.py","utf8");
const html=py.match(/INDEX_HTML = r"""\n?([\s\S]*?)\n"""/)[1];
const jsErrors=[]; const vc=new VirtualConsole();
vc.on("jsdomError",e=>jsErrors.push(String(e.detail&&e.detail.message||e.message||e)));
const state={ok:true,open:true,epoch:1,pages:5,filename:"t.pdf",path:"",size_kb:10,
  sizes:[[600,800],[600,800],[600,800],[600,800],[600,800]],can_undo:false,dirty:false,rotations:[0,0,0,0,0]};
let compressCall=null;
const dom=new JSDOM(html,{runScripts:"dangerously",url:"http://127.0.0.1:8123/",pretendToBeVisual:true,virtualConsole:vc,
  beforeParse(window){
    window.IntersectionObserver=class{constructor(cb){this.cb=cb;}observe(t){setTimeout(()=>this.cb([{target:t,isIntersecting:true}]),0);}unobserve(){}disconnect(){}};
    window.URL.createObjectURL=()=>"blob:x"; window.URL.revokeObjectURL=()=>{};
    const jsonRes=o=>({ok:true,headers:{get:h=>h==="Content-Type"?"application/json":null},json:async()=>o,blob:async()=>({size:3})});
    window.fetch=async(u,opts)=>{u=String(u);
      if(u.includes("/api/ping"))return jsonRes({ok:true,epoch:state.epoch});
      if(u.includes("/api/state"))return jsonRes(state);
      if(u.includes("/api/spans"))return jsonRes({ok:true,spans:[]});
      if(u.includes("/api/page"))return{ok:true,headers:{get:h=>h==="Content-Type"?"image/jpeg":null},blob:async()=>({size:3})};
      if(u.includes("/api/compress")){compressCall=JSON.parse(opts.body);state.epoch++;return jsonRes({ok:true,level:compressCall.level,target_kb:700,met_target:true,before_kb:100,after_kb:50,saved_pct:50});}
      return jsonRes({ok:true,pages:state.pages});};
  }});
const w=dom.window, doc=dom.window.document;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const fails=[]; const check=(n,c,e="")=>{console.log((c?"PASS ":"FAIL ")+n+(c?"":" — "+e));if(!c)fails.push(n);};
let scrolled=[];
(async()=>{
  await sleep(250);
  doc.querySelectorAll(".stage").forEach(s=>{ s.scrollIntoView=()=>{}; });
  // stub viewer.scrollTo
  const v=doc.getElementById("viewer"); v.scrollTo=(o)=>scrolled.push(Math.round(o.top));
  check("boot ok",jsErrors.length===0,jsErrors[0]);
  check("no compLevel select",!doc.getElementById("compLevel"));
  check("prev/next exist",!!doc.getElementById("pagePrev")&&!!doc.getElementById("pageNext"));
  check("prev disabled on page1",doc.getElementById("pagePrev").disabled===true);
  // next page
  doc.getElementById("pageNext").dispatchEvent(new w.MouseEvent("click",{bubbles:true}));
  await sleep(20);
  check("Next page scrolls viewer",scrolled.length>0,"scrolled="+JSON.stringify(scrolled));
  // open compress popover
  const cb=doc.getElementById("compressBtn"); cb.getBoundingClientRect=()=>({left:500,bottom:50,right:560,top:10,width:60,height:40});
  cb.dispatchEvent(new w.MouseEvent("click",{bubbles:true}));
  await sleep(20);
  const pop=doc.getElementById("compPop");
  check("popover opens",!!pop);
  check("popover has 3 levels",pop&&pop.querySelectorAll(".opt").length===3,pop?pop.querySelectorAll(".opt").length:"none");
  check("medium is current by default",!!pop&&pop.querySelector('.opt[data-v="medium"]').classList.contains("cur"));
  // pick High
  pop.querySelector('.opt[data-v="high"]').dispatchEvent(new w.MouseEvent("click",{bubbles:true}));
  await sleep(30);
  check("compress called with high",compressCall&&compressCall.level==="high",JSON.stringify(compressCall));
  check("popover closed after pick",!doc.getElementById("compPop"));
  check("no JS errors",jsErrors.length===0,jsErrors[0]);
  console.log("\n"+fails.length+" failed");
  try{ dom.window.close(); }catch(e){}
  process.exit(fails.length?1:0);
})();
