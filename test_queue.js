// One single queue: a pull of 10 gives 10, and grades only reorder the file.
const fs = require("fs"), { JSDOM } = require("jsdom");
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
const sleep = ms => new Promise(r => setTimeout(r, ms));
let pass = 0, fail = 0;
const ok = (c, name, extra) => { if (c) { pass++; console.log("  ✓ " + name); } else { fail++; console.log("  ✗ " + name, extra === undefined ? "" : extra); } };

setTimeout(async () => {
  const w = dom.window, d = w.document;
  const deckOf = n => w.eval(`WORDS.slice(0, ${n}).map(x => x.hz)`);

  console.log("\n— asking for ten cards");
  w.eval(`S.settings.perSession = 10; S.deck = ${JSON.stringify(deckOf(30))}; S.study = {date:"", queue:[]}; save();`);
  w.eval("startDeck()"); await sleep(80);
  ok(w.eval("studyQueue().length") === 10, "a pull of 10 yields 10", w.eval("studyQueue().length"));
  ok(w.eval("S.deck.length") === 20, "the rest of the file is untouched", w.eval("S.deck.length"));

  console.log("\n— a half-finished pile is topped up, not resumed");
  w.eval(`S.settings.perSession = 10; S.deck = ${JSON.stringify(deckOf(30))}; S.study = {date:"", queue: []}; save();`);
  w.eval("S.study.queue = S.deck.splice(0, 6); save()");
  w.eval("startDeck()"); await sleep(80);
  ok(w.eval("studyQueue().length") === 10, "six left + six asked = ten in the session", w.eval("studyQueue().length"));
  ok(w.eval("S.deck.length") === 20, "the four extra came off the file", w.eval("S.deck.length"));

  console.log("\n— where a card goes after a grade");
  const first = w.eval("studyQueue()[0]");
  w.eval(`grade("wrong")`); await sleep(60);
  ok(w.eval(`studyQueue()[studyQueue().length-1]`) === first, "missed → back of today's pile");
  ok(w.eval(`studyQueue().includes(${JSON.stringify(first)})`), "and it will come round again today");

  const second = w.eval("studyQueue()[0]");
  w.eval(`grade("close")`); await sleep(60);
  ok(w.eval("S.deck[0]") === second, "almost → first of the cards not taken today", w.eval("S.deck[0]"));
  ok(!w.eval(`studyQueue().includes(${JSON.stringify(second)})`), "so it will not come back today");

  const third = w.eval("studyQueue()[0]");
  w.eval(`grade("right")`); await sleep(60);
  ok(w.eval("S.deck[S.deck.length-1]") === third, "right → very bottom of the whole file");

  console.log("\n— restart re-cuts exactly the number asked");
  w.eval("S.settings.perSession = 10; restartStudy()"); await sleep(80);
  ok(w.eval("studyQueue().length") === 10, "restart gives ten again", w.eval("studyQueue().length"));
  ok(w.eval("new Set(S.deck.concat(studyQueue())).size") === 30,
     "no card lost or duplicated", w.eval("S.deck.length + studyQueue().length"));

  console.log("\n=== " + pass + " passed / " + (pass + fail) + " ===");
  process.exit(fail ? 1 : 0);
}, 900);
