import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const py=fs.readFileSync("pdf_editor_app.py","utf8");
const html=py.match(/INDEX_HTML = r"""\n?([\s\S]*?)\n"""/)[1];
const jsErrors=[]; const vc=new VirtualConsole();
vc.on("jsdomError",e=>jsErrors.push(String(e.detail&&e.detail.message||e.message||e)));
const state={ok:true,open:true,epoch:1,pages:1,filename:"t.pdf",path:"",size_kb:10,sizes:[[600,800]],can_undo:false,dirty:false,rotations:[0]};
const sampleText="Order ID rajesh nanda\nrajesh again\nanother rajesh line";
const dom=new JSDOM(html,{runScripts:"dangerously",url:"http://127.0.0.1:8123/",pretendToBeVisual:true,virtualConsole:vc,
  beforeParse(window){
    window.IntersectionObserver=class{constructor(cb){this.cb=cb;}observe(t){setTimeout(()=>this.cb([{target:t,isIntersecting:true}]),0);}unobserve(){}disconnect(){}};
    window.URL.createObjectURL=()=>"blob:x"; window.URL.revokeObjectURL=()=>{};
    const jsonRes=o=>({ok:true,headers:{get:h=>h==="Content-Type"?"application/json":null},json:async()=>o,blob:async()=>({size:3})});
    window.fetch=async(u)=>{u=String(u);
      if(u.includes("/api/ping"))return jsonRes({ok:true,epoch:state.epoch});
      if(u.includes("/api/state"))return jsonRes(state);
      if(u.includes("/api/spans"))return jsonRes({ok:true,spans:[]});
      if(u.includes("/api/text"))return jsonRes({ok:true,text:sampleText});
      if(u.includes("/api/page"))return{ok:true,headers:{get:h=>h==="Content-Type"?"image/jpeg":null},blob:async()=>({size:3})};
      return jsonRes({ok:true,pages:1});};
  }});
const w=dom.window, doc=dom.window.document;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const fails=[]; const check=(n,c,e="")=>{console.log((c?"PASS ":"FAIL ")+n+(c?"":" — "+e));if(!c)fails.push(n);};
(async()=>{
  await sleep(250);
  w.openTextModal(); await sleep(60);
  const tf=doc.getElementById("txtFind");
  check("text modal open + find box",!!tf);
  tf.focus();
  // type 'r' then 'a' simulating keystrokes
  tf.value="r"; tf.dispatchEvent(new w.Event("input",{bubbles:true})); await sleep(10);
  check("focus stays in find box after 1st letter", doc.activeElement===tf, "active="+(doc.activeElement&&doc.activeElement.id));
  check("count shows after typing", doc.getElementById("txtFindCount").textContent.includes("/"), doc.getElementById("txtFindCount").textContent);
  tf.value="ra"; tf.dispatchEvent(new w.Event("input",{bubbles:true})); await sleep(10);
  check("focus stays after 2nd letter", doc.activeElement===tf, "active="+(doc.activeElement&&doc.activeElement.id));
  tf.value="rajesh"; tf.dispatchEvent(new w.Event("input",{bubbles:true})); await sleep(10);
  check("focus stays after full word", doc.activeElement===tf, "active="+(doc.activeElement&&doc.activeElement.id));
  check("3 hits for rajesh", doc.getElementById("txtFindCount").textContent==="1/3", doc.getElementById("txtFindCount").textContent);
  // Enter steps and keeps focus
  tf.dispatchEvent(new w.KeyboardEvent("keydown",{key:"Enter",bubbles:true})); await sleep(10);
  check("focus stays after Enter step", doc.activeElement===tf, "active="+(doc.activeElement&&doc.activeElement.id));
  check("count advanced to 2/3", doc.getElementById("txtFindCount").textContent==="2/3", doc.getElementById("txtFindCount").textContent);
  check("no JS errors",jsErrors.length===0,jsErrors[0]);
  console.log("\n"+fails.length+" failed");
  try{ dom.window.close(); }catch(e){}
  process.exit(fails.length?1:0);
})();
