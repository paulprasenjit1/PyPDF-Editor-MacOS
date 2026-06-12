// Headless UI harness: loads the real app HTML/JS in jsdom with a stubbed
// backend, then exercises the main UI flows (welcome, organize, copy, save).
// Run: node ui_test.mjs <path-to-pdf_editor_app.py>
import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";

const py = fs.readFileSync(process.argv[2] || "pdf_editor_app.py", "utf8");
const m = py.match(/INDEX_HTML = r"""\n?([\s\S]*?)\n"""/);
if (!m) { console.error("FAIL could not extract INDEX_HTML"); process.exit(1); }
const html = m[1];

const fails = [];
const check = (name, cond, extra="") => {
  console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : (extra ? " — " + extra : "")));
  if (!cond) fails.push(name);
};

const jsErrors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", e => jsErrors.push(String(e.detail && e.detail.message || e.message || e)));

const state = { ok:true, open:true, epoch:1, pages:3, filename:"test.pdf", path:"/tmp/test.pdf",
  size_kb:42.5, sizes:[[595,842],[595,842],[842,595]], can_undo:false, dirty:false, rotations:[0,0,0] };
const calls = { reorder:null, create:null, close:0 };

const dom = new JSDOM(html, {
  runScripts: "dangerously", url: "http://127.0.0.1:8123/", pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(window) {
    window.IntersectionObserver = class {
      constructor(cb){ this.cb = cb; }
      observe(t){ setTimeout(()=>this.cb([{ target:t, isIntersecting:true }]), 0); }
      unobserve(){} disconnect(){}
    };
    window.URL.createObjectURL = () => "blob:fake-" + Math.random();
    window.URL.revokeObjectURL = () => {};
    window.confirm = () => true;
    const jsonRes = o => ({ ok:true,
      headers:{ get:h => h==="Content-Type" ? "application/json" : null },
      json: async () => o, blob: async () => ({ size: 3 }) });
    window.fetch = async (u, opts) => {
      u = String(u);
      if (u.includes("/api/ping"))  return jsonRes({ ok:true, epoch: state.epoch });
      if (u.includes("/api/state")) return jsonRes(state);
      if (u.includes("/api/about")) return jsonRes({ ok:true, version:"test", engine:"PyMuPDF x", python:"3", started:"now" });
      if (u.includes("/api/spans")) return jsonRes({ ok:true, spans:[] });
      if (u.includes("/api/page"))  return { ok:true,
        headers:{ get:h => h==="Content-Type" ? "image/jpeg" : null }, blob: async () => ({ size:3 }) };
      if (u.includes("/api/reorder")) { calls.reorder = JSON.parse(opts.body); state.epoch++; return jsonRes({ ok:true, pages:state.pages }); }
      if (u.includes("/api/create_from_images")) { calls.create = JSON.parse(opts.body); state.epoch++; return jsonRes({ ok:true, pages:1 }); }
      if (u.includes("/api/close")) { calls.close++; state.open=false; state.epoch++; return jsonRes({ ok:true, epoch:state.epoch }); }
      return jsonRes({ ok:true, pages: state.pages });
    };
  },
});

const w = dom.window, d = w.document;
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  await sleep(300);   // let boot() run

  check("no JS errors at boot", jsErrors.length === 0, jsErrors[0]);
  check("welcome buttons exist", d.querySelectorAll(".welcome .big").length === 2);
  check("document loaded: 3 stages", d.querySelectorAll(".stage").length === 3,
        "stages=" + d.querySelectorAll(".stage").length);
  check("Organize button enabled", !d.getElementById("orgBtn").disabled);

  // ---- Organize Pages ----
  d.getElementById("orgBtn").dispatchEvent(new w.Event("click", { bubbles:true }));
  w.openOrganize && w.openOrganize();   // belt and braces if inline onclick didn't fire
  await sleep(150);
  const overlayShown = d.getElementById("overlay").classList.contains("show");
  check("organize: modal opens", overlayShown);
  const thumbs = d.querySelectorAll("#thumbs .thumb");
  check("organize: 3 thumbnails", thumbs.length === 3, "thumbs=" + thumbs.length);
  await sleep(100);
  const imgs = d.querySelectorAll("#thumbs img");
  const loaded = [...imgs].filter(i => i.src && i.src.startsWith("blob:")).length;
  check("organize: thumbnails lazy-loaded", loaded === 3, "loaded=" + loaded);

  // rotate page 0 twice (-> 180), then apply
  w.orgRotate(0); await sleep(80); w.orgRotate(0); await sleep(80);
  check("organize: rotation badge shown", d.querySelector("#thumbs .badge").textContent.includes("180"));
  const applyBtn = [...d.querySelectorAll(".mfoot button")].find(b => /Apply/.test(b.textContent));
  check("organize: Apply button present", !!applyBtn);
  applyBtn.dispatchEvent(new w.Event("click", { bubbles:true }));
  await sleep(150);
  check("organize: reorder API called", !!calls.reorder, JSON.stringify(calls.reorder));
  if (calls.reorder) {
    check("organize: payload order+rotations", JSON.stringify(calls.reorder.order) === "[0,1,2]"
      && JSON.stringify(calls.reorder.rotations) === "[180,0,0]", JSON.stringify(calls.reorder));
  }
  check("organize: modal closed after apply", !d.getElementById("overlay").classList.contains("show"));

  // ---- Copy Pages modal ----
  w.openCopyPages(); await sleep(120);
  check("copy: modal opens", d.getElementById("overlay").classList.contains("show"));
  check("copy: 3 thumbnails", d.querySelectorAll(".modal .thumb, #modal .thumb").length === 3
        || d.querySelectorAll(".thumb").length === 3);
  w.copyToggle(1); await sleep(50);
  check("copy: selection marks", d.querySelectorAll(".thumb.picked").length === 1);
  w.closeModal();

  // ---- image quality dialog ----
  const file = new w.File(["fake-image-bytes"], "a.png", { type: "image/png" });
  const p = w.createFromImageFiles([file]);
  await sleep(80);
  check("images: quality dialog opens", d.getElementById("overlay").classList.contains("show"));
  const go = d.getElementById("iqGo");
  check("images: Standard preselected", (d.querySelector('input[name="iq"]:checked')||{}).value === "normal");
  d.querySelector('input[name="iq"][value="small"]').checked = true;
  go.dispatchEvent(new w.Event("click", { bubbles:true }));
  await sleep(150);
  check("images: API got quality=small", calls.create && calls.create.quality === "small",
        JSON.stringify(calls.create));

  // ---- knockout hidden ----
  const ko = d.getElementById("sigKnockout");
  check("signature: knockout present but hidden", !!ko && ko.closest("label").style.display === "none");
  check("signature: knockout unchecked by default", !ko.checked);

  check("no JS errors at end", jsErrors.length === 0, jsErrors[0]);
  console.log("\n%d UI checks failed", fails.length);
  process.exit(fails.length ? 1 : 0);
})();
