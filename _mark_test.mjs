import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const py=fs.readFileSync("pdf_editor_app.py","utf8");
const html=py.match(/INDEX_HTML = r"""\n?([\s\S]*?)\n"""/)[1];
const jsErrors=[]; const vc=new VirtualConsole();
vc.on("jsdomError",e=>jsErrors.push(String(e.detail&&e.detail.message||e.message||e)));
const state={ok:true,open:true,epoch:1,pages:1,filename:"t.pdf",path:"",size_kb:10,sizes:[[600,800]],can_undo:false,dirty:false,rotations:[0]};
const sampleText="Bharat chandra Paul\nbharat again line\nBHARAT third\nno match here\n<script>x</script> & special";
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
  const tf=doc.getElementById("txtFind"), box=doc.getElementById("txtOut");
  check("text modal open",!!tf&&!!box);
  check("special chars escaped (no script injection)", box.innerHTML.includes("&lt;script&gt;")&&!box.querySelector("script"));
  tf.focus();
  tf.value="bharat"; tf.dispatchEvent(new w.Event("input",{bubbles:true}));
  await sleep(160); // wait past debounce
  const marks=box.querySelectorAll("mark");
  check("all 3 matches highlighted", marks.length===3, "marks="+marks.length);
  check("exactly one current (orange)", box.querySelectorAll("mark.cur").length===1);
  check("first match is current", box.querySelector("mark.cur").id==="thit0");
  check("count 1/3", doc.getElementById("txtFindCount").textContent==="1/3", doc.getElementById("txtFindCount").textContent);
  check("focus stays in find box", doc.activeElement===tf, "active="+(doc.activeElement&&doc.activeElement.id));
  // step next
  tf.dispatchEvent(new w.KeyboardEvent("keydown",{key:"Enter",bubbles:true})); await sleep(10);
  check("current moved to thit1", box.querySelector("mark.cur").id==="thit1");
  check("count 2/3", doc.getElementById("txtFindCount").textContent==="2/3");
  check("focus still in box after Enter", doc.activeElement===tf);
  // clear
  tf.value=""; tf.dispatchEvent(new w.Event("input",{bubbles:true})); await sleep(160);
  check("no marks after clearing", box.querySelectorAll("mark").length===0);
  check("no JS errors",jsErrors.length===0,jsErrors[0]);
  console.log("\n"+fails.length+" failed");
  try{ dom.window.close(); }catch(e){}
  process.exit(fails.length?1:0);
})();
