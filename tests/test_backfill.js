// The deck gets back every word of the lessons already finished.
const fs = require("fs"), { JSDOM } = require("jsdom");
const STATE = JSON.parse(fs.readFileSync("/home/ymz/.hermes/profiles/chineseteacher/app/state/2f2cbbb990eef550.json", "utf8"));
const dom = new JSDOM(fs.readFileSync("/home/ymz/.hermes/profiles/chineseteacher/app/index.html", "utf8"), {
  runScripts: "dangerously", pretendToBeVisual: true, url: "http://x/",
  beforeParse(w) {
    w.scrollTo = () => {}; w.confirm = () => true;
    w.fetch = async () => ({ ok: true, json: async () => ({ authed: true }) });
    w.speechSynthesis = { speak() {}, cancel() {}, getVoices: () => [] };
    w.SpeechSynthesisUtterance = class {};
    w.AudioContext = class { constructor() { this.destination = {}; }
      createOscillator() { const o = { frequency: {}, connect: () => o, start() {}, stop() {} }; return o; }
      createGain() { const g = { gain: { setValueAtTime() {} }, connect: () => g }; return g; } };
    w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {}, set: () => true });
    w.HTMLCanvasElement.prototype.getBoundingClientRect = () => ({ width: 300, height: 260, left: 0, top: 0 });
  }
});
let pass = 0, fail = 0;
const ok = (c, n, x) => { if (c) { pass++; console.log("  ✓ " + n); } else { fail++; console.log("  ✗ " + n, x === undefined ? "" : x); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

setTimeout(async () => {
  const w = dom.window;
  // the real situation: 14 lessons done, an empty deck, 6 cards in today's pile
  w.eval(`S = ${JSON.stringify(STATE.state)}; save();`);
  const before = w.eval("({deck: S.deck.length, pile: studyQueue().length, lessons: Object.keys(S.lessons).filter(k => S.lessons[k].completed).length})");
  console.log("avant:", JSON.stringify(before));
  const added = w.eval("backfillDeck()");
  const after = w.eval("({deck: S.deck.length, pile: studyQueue().length})");
  console.log("ajoutées:", added, "→", JSON.stringify(after));
  ok(added > 30, "the finished lessons put dozens of cards back", added);
  ok(w.eval("S.deck.length") === added, "the deck now holds them", w.eval("S.deck.length"));
  ok(new Set(w.eval("S.deck")).size === w.eval("S.deck.length"), "no duplicates");
  ok(w.eval("studyQueue().every(hz => !S.deck.includes(hz))"), "nothing is in the pile and in the deck at once");
  ok(w.eval("backfillDeck()") === 0, "running it again changes nothing");
  ok(w.eval("S.deck.every(hz => WMAP[hz])"), "every card is a word the app knows");
  console.log("  deck:", w.eval("S.deck.slice(0,12).join(' ')"), "…");
  console.log("\n=== " + pass + " passed / " + (pass + fail) + " ===");
  process.exit(fail ? 1 : 0);
}, 900);
