import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const py = fs.readFileSync("pdf_editor_app.py","utf8");
const html = py.match(/INDEX_HTML = r"""\n?([\s\S]*?)\n"""/)[1];
const jsErrors=[]; const vc=new VirtualConsole();
vc.on("jsdomError",e=>jsErrors.push(String(e.detail&&e.detail.message||e.message||e)));
const state={ok:true,open:true,epoch:1,pages:2,filename:"t.pdf",path:"",size_kb:10,
  sizes:[[600,800],[600,800]],can_undo:false,dirty:false,rotations:[0,0]};
let signCall=null;
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
      if(u.includes("/api/sign")){signCall=JSON.parse(opts.body);state.epoch++;return jsonRes({ok:true});}
      return jsonRes({ok:true,pages:state.pages});
    };
  }});
const w=dom.window,d=d2=>dom.window.document;
const doc=dom.window.document;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function stubRect(el,rect){ el.getBoundingClientRect=()=>({left:rect.x,top:rect.y,right:rect.x+rect.w,bottom:rect.y+rect.h,width:rect.w,height:rect.h,x:rect.x,y:rect.y}); 
  Object.defineProperty(el,"clientWidth",{value:rect.w,configurable:true});
  Object.defineProperty(el,"clientHeight",{value:rect.h,configurable:true});
  Object.defineProperty(el,"offsetWidth",{get(){return parseFloat(el.style.width)||rect.w;},configurable:true});
  Object.defineProperty(el,"offsetHeight",{get(){return parseFloat(el.style.height)||rect.h;},configurable:true});
}
const fails=[]; const check=(n,c,e="")=>{console.log((c?"PASS ":"FAIL ")+n+(c?"":" — "+e));if(!c)fails.push(n);};
function mev(type,x,y,t){return new w.MouseEvent(type,{clientX:x,clientY:y,button:0,bubbles:true});}
(async()=>{
  await sleep(300);
  check("boot ok",jsErrors.length===0,jsErrors[0]);
  const stage=doc.querySelector('.stage[data-page="0"]');
  check("stage exists",!!stage);
  const img=stage.querySelector("img");
  stubRect(img,{x:100,y:100,w:600,h:800});
  // scale
  w.scaleByPage[0]=1;  // 600px display / 600pt
  // load signature + edit + sign mode
  w.sigB64="iVBORw0KGgo=";  // tiny fake
  w.setEdit(true); w.setSign(true);
  await sleep(20);
  check("signMode on",w.signMode===true,"signMode="+w.signMode);
  // draw a box from (200,200) to (360,300) in client coords -> image-relative (100,100)-(260,200)
  stage.dispatchEvent(mev("mousedown",200,200));
  stage.dispatchEvent(mev("mousemove",360,300));
  w.dispatchEvent(mev("mouseup",360,300));
  await sleep(20);
  const prev=stage.querySelector(".sigprev");
  check("preview appears after draw",!!prev, "no .sigprev");
  if(prev){
    stubRect(prev,{x:0,y:0,w:160,h:100});
    const left0=parseFloat(prev.style.left), top0=parseFloat(prev.style.top);
    check("preview has size",parseFloat(prev.style.width)>10 && parseFloat(prev.style.height)>10,
       "w="+prev.style.width+" h="+prev.style.height);
    // drag it by (40,30)
    prev.dispatchEvent(mev("mousedown",300,300));
    w.dispatchEvent(mev("mousemove",340,330));
    w.dispatchEvent(mev("mouseup",340,330));
    await sleep(20);
    const left1=parseFloat(prev.style.left), top1=parseFloat(prev.style.top);
    check("preview moves on drag",(left1!==left0||top1!==top0),"left "+left0+"->"+left1+" top "+top0+"->"+top1);
    // place
    const place=doc.getElementById("sigPlace");
    check("Place button exists",!!place);
    place && place.dispatchEvent(mev("click",0,0));
    await sleep(40);
    check("Place calls /api/sign",!!signCall,JSON.stringify(signCall));
    if(signCall) check("sign rect plausible",Array.isArray(signCall.rect)&&signCall.rect.length===4,JSON.stringify(signCall.rect));
  }
  check("no JS errors",jsErrors.length===0,jsErrors[0]);
  console.log("\n"+fails.length+" failed");
  process.exit(fails.length?1:0);
})();
