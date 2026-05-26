/* ════════════════════════════════════════════════
   TELOS — enterprise capability site
   i18n · reveal · estimator · lead form
   ════════════════════════════════════════════════ */

/* ───────── i18n dictionary ───────── */
const I18N = {
  zh: {
    "nav.problem": "成本问题",
    "nav.proof": "效果数据",
    "nav.protocol": "工作原理",
    "nav.matrix": "支持范围",
    "nav.roadmap": "路线图",
    "nav.cta": "预约洽谈",

    "hero.eyebrow": "面向 AI Agent 的成本感知推理基础设施",
    "hero.title": '把 AI Agent 推理账单<br/><span class="hl">砍掉 90%</span>',
    "hero.lead":
      "TELOS 用一套稳定前缀协议，让多轮 Agent 的每一次调用都命中缓存。不重写、不压缩、不改你的业务代码——账单直接以绝对美元计量地下降。",
    "hero.ctaPrimary": "预约企业洽谈",
    "hero.ctaSecondary": "查看实测数据",
    "hero.stat1": "真实 6 轮会话成本下降",
    "hero.stat2": "同一会话的实付账单",
    "hero.stat3": "协议唯一不变量",

    "trust.label": "已在以下模型与推理栈上验证",
    "trust.org": "由清华大学 LEAP Lab 研发 —— 专注机器学习、多模态与具身智能的研究团队",

    "problem.kicker": "成本问题",
    "problem.title": "凌晨两点，钱去哪了？",
    "problem.lead":
      "大多数 Agent 框架每一轮都把同一段长提示按原价重新计费。问题不在「token 倍数」，而在月底那张真实的服务器账单。TELOS 把它从倍数叙事拉回绝对美元。",
    "problem.thMode": "模式",
    "problem.thRaw": "原始输入 token",
    "problem.thCache": "缓存命中",
    "problem.thCost": "6 轮成本",
    "problem.r1mode": "直通（今天的默认）",
    "problem.r2mode": "接入 TELOS",
    "problem.note":
      '把这放大到 1,000 个会话：<b>$362 → $26</b>。倍数可以被包装，美元不会说谎。',

    "proof.kicker": "效果数据",
    "proof.title": "省下的每一分钱，都钉在绝对美元上",
    "proof.lead":
      "我们只用一个口径汇报效果：绝对 $/已解决查询。下列数字来自真实会话与受控 A/B/C/D 实验，可在交付时复现。",
    "proof.s1": "真实 6 轮会话 · 成本下降",
    "proof.s2": "受控 48 次调用 · 净下降（净省 $2.16）",
    "proof.s3": "1,000 个会话规模下的月账单变化",
    "proof.estTitle": "省钱估算器",
    "proof.estLead": "按你团队的月会话数与单会话基准成本，估算接入后的月度节省。",
    "proof.estSessions": "每月会话数",
    "proof.estBaseline": "单会话基准成本",
    "proof.estWithout": "未接入",
    "proof.estWith": "接入 TELOS（估算）",
    "proof.estSave": "预计每月节省",
    "proof.estFine": "估算基于实测 −92.3% 的节省率，仅供参考；正式数字以你的真实流量复现为准。",

    "protocol.kicker": "工作原理",
    "protocol.title": "不是压缩，而是永不破坏前缀",
    "protocol.lead":
      "大多数框架把 KV 缓存当成推理引擎随机施舍的礼物。TELOS 把它反过来——缓存复用是提示结构本身的性质：只要不触碰已提交的字节，缓存就无法失效。",
    "protocol.dropTag": "DROP — 逐轮擦除",
    "protocol.foldTag": "FOLD — 可缓存 / 可折叠",
    "protocol.pinTag": "PIN — 终身留存的基座",
    "protocol.pinDesc":
      "工具定义、系统提示、当前问题。永久留存、永不淘汰——每个请求前缀哈希的不可变基座。",
    "protocol.foldDesc":
      "对话历史、工具结果、大文档。可缓存、可压缩；压力下被摘要替换，而 PIN 前缀字节保持不动。",
    "protocol.dropDesc":
      "时间戳、当前目录、git 状态、进程号。完全排除在前缀哈希之外，绝不污染上游字节。",
    "protocol.invariant":
      '顺序不变量是绝对的：<b>PIN* → FOLD* → DROP*</b>。这是唯一能赢得缓存的结构规则——其余都是实现细节。',

    "caps.kicker": "能力地图",
    "caps.title": "企业接入后得到什么",
    "caps.lead": "TELOS 是一层适配器驱动的流量基础设施，落在你的 Agent 与模型之间，对业务代码零侵入。",

    "matrix.kicker": "支持范围",
    "matrix.title": "覆盖你正在用的整条推理栈",
    "matrix.harness": "Agent 框架",
    "matrix.models": "前沿模型",
    "matrix.frameworks": "推理框架",
    "matrix.note":
      "需要其它框架或模型后端？TELOS 由适配器驱动——保持同一套 IR，新增一个引擎 / 框架适配器即可，无需重写 Agent 逻辑。",

    "onboard.kicker": "如何接入",
    "onboard.title": "三步把 TELOS 接进你的团队",
    "onboard.lead": "一次轻量的企业交付流程——从评估到持续优化，全程由我们的团队陪同。",
    "onboard.s1title": "成本评估",
    "onboard.s1desc":
      "我们用你的真实流量样本，给出一份绝对美元口径的节省评估报告，明确接入后能省多少。",
    "onboard.s2title": "网关接入",
    "onboard.s2desc":
      "本地网关落在 Agent 与模型之间，自动接管调用链。无需改业务代码，故障时自动回退直通。",
    "onboard.s3title": "持续优化",
    "onboard.s3desc":
      "可观测仪表盘按调用拆解实付与反事实成本，团队随你的流量规模持续调优。",

    "roadmap.kicker": "路线图",
    "roadmap.title": "TELOS 的演进方向",

    "contact.kicker": "企业合作",
    "contact.title": "与 TELOS 团队洽谈",
    "contact.lead":
      "TELOS 面向企业提供合作接入。留下联系方式，我们会在一个工作日内联系你，安排成本评估与演示。",
    "contact.p1": "基于你真实流量的绝对美元节省评估",
    "contact.p2": "私有部署与定制适配器支持",
    "contact.p3": "由清华 LEAP Lab 核心团队直接对接",
    "contact.fallback": "或直接邮件联系：",
    "contact.fName": "姓名",
    "contact.fCompany": "公司",
    "contact.fEmail": "工作邮箱",
    "contact.fVolume": "每月模型调用量",
    "contact.vChoose": "请选择（选填）",
    "contact.v1": "少于 10 万次",
    "contact.v2": "10 万 – 100 万次",
    "contact.v3": "100 万 – 1000 万次",
    "contact.v4": "超过 1000 万次",
    "contact.fMessage": "留言（选填）",
    "contact.submit": "提交合作意向",
    "contact.sending": "提交中…",
    "contact.ok": "已收到，感谢！我们会尽快与你联系。",
    "contact.errFields": "请填写姓名、公司与有效的工作邮箱。",
    "contact.errNet": "提交失败，请稍后重试，或直接邮件联系我们。",

    "footer.tag": "面向 AI Agent 的成本感知推理基础设施。",
    "footer.org": "清华大学 LEAP Lab",
    "footer.rights": "保留所有权利",
  },

  en: {
    "nav.problem": "The Problem",
    "nav.proof": "The Numbers",
    "nav.protocol": "How It Works",
    "nav.matrix": "Coverage",
    "nav.roadmap": "Roadmap",
    "nav.cta": "Talk to us",

    "hero.eyebrow": "Cost-aware inference infrastructure for AI agents",
    "hero.title": 'Cut your AI agent<br/>inference bill by <span class="hl">90%</span>',
    "hero.lead":
      "TELOS uses one stable-prefix protocol so every turn of a multi-turn agent hits the cache. No rewrite, no compression, no changes to your code — the bill drops, measured in absolute dollars.",
    "hero.ctaPrimary": "Book an enterprise call",
    "hero.ctaSecondary": "See the numbers",
    "hero.stat1": "Cost cut on a real 6-turn session",
    "hero.stat2": "Actual bill for the same session",
    "hero.stat3": "The protocol's one invariant",

    "trust.label": "Validated across these models and inference stacks",
    "trust.org":
      "Built by LEAP Lab @ Tsinghua University — a research group focused on machine learning, multimodal and embodied intelligence",

    "problem.kicker": "The Problem",
    "problem.title": "2 a.m. — where did all the money go?",
    "problem.lead":
      "Most agent frameworks rebill the same long prompt at full price every turn. The issue isn't an 'X× tokens' ratio — it's the real server invoice at month end. TELOS reframes it from ratio theater into absolute dollars.",
    "problem.thMode": "Mode",
    "problem.thRaw": "Raw input tokens",
    "problem.thCache": "Cache read",
    "problem.thCost": "Cost / 6 turns",
    "problem.r1mode": "Passthrough (today's default)",
    "problem.r2mode": "With TELOS",
    "problem.note":
      'Scale that to 1,000 sessions: <b>$362 → $26</b>. Ratios can be gamed; dollars can&apos;t.',

    "proof.kicker": "The Numbers",
    "proof.title": "Every cent saved, pinned to an absolute dollar",
    "proof.lead":
      "We report results in one unit only: absolute $/query-resolved. The figures below come from real sessions and a controlled A/B/C/D run — reproducible on delivery.",
    "proof.s1": "Real 6-turn session · cost reduction",
    "proof.s2": "Controlled 48-call run · net reduction (net −$2.16)",
    "proof.s3": "Monthly bill change at 1,000-session scale",
    "proof.estTitle": "Savings estimator",
    "proof.estLead":
      "Estimate your monthly savings from your team's session count and baseline cost per session.",
    "proof.estSessions": "Monthly sessions",
    "proof.estBaseline": "Baseline cost / session",
    "proof.estWithout": "Without TELOS",
    "proof.estWith": "With TELOS (est.)",
    "proof.estSave": "Estimated monthly savings",
    "proof.estFine":
      "Estimate uses the measured −92.3% savings rate, for reference only. Real figures are confirmed by reproducing on your own traffic.",

    "protocol.kicker": "How It Works",
    "protocol.title": "Not compression — never breaking the prefix",
    "protocol.lead":
      "Most frameworks treat the KV cache as a gift the inference engine may or may not give you. TELOS inverts this: cache reuse is a structural property of the prompt — if you never touch submitted bytes, the cache cannot be invalidated.",
    "protocol.dropTag": "DROP — wiped each turn",
    "protocol.foldTag": "FOLD — cacheable / foldable",
    "protocol.pinTag": "PIN — the carved base, lasts a lifetime",
    "protocol.pinDesc":
      "Tool defs, system prompt, current question. Permanent, never evicted — the immutable base of every request's prefix hash.",
    "protocol.foldDesc":
      "Conversation history, tool results, large docs. Cacheable and compactable; replaced by a summary under pressure while PIN prefix bytes stay untouched.",
    "protocol.dropDesc":
      "Timestamps, CWD, git status, PIDs. Excluded entirely from the prefix hash — never contaminates upstream bytes.",
    "protocol.invariant":
      'The ordering invariant is absolute: <b>PIN* → FOLD* → DROP*</b>. This is the one structural rule that wins the cache — everything else is implementation detail.',

    "caps.kicker": "Capability map",
    "caps.title": "What you get when you onboard",
    "caps.lead":
      "TELOS is an adapter-driven traffic layer that sits between your agents and the models — zero intrusion into your business code.",

    "matrix.kicker": "Coverage",
    "matrix.title": "Covers the whole inference stack you already run",
    "matrix.harness": "Agent harnesses",
    "matrix.models": "Frontier models",
    "matrix.frameworks": "Inference frameworks",
    "matrix.note":
      "Need another harness or model backend? TELOS is adapter-driven — keep the same IR and add an engine/harness adapter without rewriting your agent logic.",

    "onboard.kicker": "Onboarding",
    "onboard.title": "Three steps to bring TELOS into your team",
    "onboard.lead":
      "A lightweight enterprise engagement — from assessment to continuous tuning, accompanied by our team throughout.",
    "onboard.s1title": "Cost assessment",
    "onboard.s1desc":
      "We run a sample of your real traffic and deliver an absolute-dollar savings report — exactly how much you'll save once onboarded.",
    "onboard.s2title": "Gateway onboarding",
    "onboard.s2desc":
      "A local gateway sits between agents and models and takes over the call chain. No code changes; passthrough fallback on failure.",
    "onboard.s3title": "Continuous tuning",
    "onboard.s3desc":
      "An observability dashboard breaks down actual vs counterfactual cost per call; our team keeps tuning as your traffic scales.",

    "roadmap.kicker": "Roadmap",
    "roadmap.title": "Where TELOS is heading",

    "contact.kicker": "Enterprise partnership",
    "contact.title": "Talk to the TELOS team",
    "contact.lead":
      "TELOS is offered to enterprises through partnership. Leave your details and we'll reach out within one business day to arrange a cost assessment and demo.",
    "contact.p1": "An absolute-dollar savings assessment on your real traffic",
    "contact.p2": "Private deployment and custom adapter support",
    "contact.p3": "Direct contact with the LEAP Lab core team",
    "contact.fallback": "Or email us directly: ",
    "contact.fName": "Name",
    "contact.fCompany": "Company",
    "contact.fEmail": "Work email",
    "contact.fVolume": "Monthly model calls",
    "contact.vChoose": "Select (optional)",
    "contact.v1": "Under 100k",
    "contact.v2": "100k – 1M",
    "contact.v3": "1M – 10M",
    "contact.v4": "Over 10M",
    "contact.fMessage": "Message (optional)",
    "contact.submit": "Submit partnership request",
    "contact.sending": "Submitting…",
    "contact.ok": "Received — thank you! We'll be in touch shortly.",
    "contact.errFields": "Please enter your name, company and a valid work email.",
    "contact.errNet": "Submission failed. Please retry shortly, or email us directly.",

    "footer.tag": "Cost-aware inference infrastructure for AI agents.",
    "footer.org": "LEAP Lab @ Tsinghua University",
    "footer.rights": "All rights reserved",
  },
};

/* ───────── data: capabilities ───────── */
const CAPS = {
  zh: [
    { ic: "⌘", t: "网关接管调用链", d: "本地网关落在 Agent 与模型之间，自动接管每一次调用，对业务代码零侵入。" },
    { ic: "≡", t: "稳定前缀协议", d: "PIN / FOLD / DROP 三色带 + 单调追加，让缓存命中成为提示结构的性质。" },
    { ic: "$", t: "绝对美元可观测性", d: "仪表盘按调用拆解实付与反事实成本，每一分节省都钉在美元上。" },
    { ic: "⇄", t: "引擎可移植", d: "一套 IR 跨 Anthropic / OpenAI / DeepSeek / vLLM / SGLang,今天上 Claude,明天换 DeepSeek。" },
    { ic: "↻", t: "回放实验", d: "录制真实会话,在多种模式下回放,做受控的成本基准测试。" },
    { ic: "⛉", t: "故障安全", d: "正确性永远第一:优化层失败时自动回退直通,绝不影响线上流量。" },
  ],
  en: [
    { ic: "⌘", t: "Gateway owns the call chain", d: "A local gateway between agents and models takes over every call, with zero intrusion into your code." },
    { ic: "≡", t: "Stable-prefix protocol", d: "PIN / FOLD / DROP banding plus monotonic append make cache hits a structural property of the prompt." },
    { ic: "$", t: "Absolute-dollar observability", d: "The dashboard breaks down actual vs counterfactual cost per call — every saving pinned to a dollar." },
    { ic: "⇄", t: "Engine-portable", d: "One IR across Anthropic / OpenAI / DeepSeek / vLLM / SGLang — Claude today, DeepSeek tomorrow." },
    { ic: "↻", t: "Replay experiments", d: "Record real sessions and replay them under mode permutations for controlled cost benchmarking." },
    { ic: "⛉", t: "Fail-safe transport", d: "Correctness first: passthrough fallback the moment an optimization layer fails — production traffic untouched." },
  ],
};

/* ───────── data: support matrix ───────── */
const MATRIX = {
  zh: {
    harness: [
      ["Claude Code", "Anthropic 原生编码 Agent · 一级支持"],
      ["OpenClaw", "开放 Agent 运行时 · 一级支持"],
      ["Hermes", "多 Agent 编排 · 一级支持"],
      ["Codex", "OpenAI 风格编码工作流 · 支持"],
    ],
    models: [
      ["Claude 4.x / 4.6+", "显式断点与预热路径"],
      ["GPT 4+ / 5.x", "prompt_cache_key 路由策略"],
      ["DeepSeek V3+", "确定性字节稳定前缀"],
    ],
    frameworks: [
      ["vLLM", "锚点 · 预热 · 探测 / 淘汰 · 部分 fork-replace"],
      ["SGLang", "锚点 · 预热 · 完整 fork-replace"],
    ],
  },
  en: {
    harness: [
      ["Claude Code", "Anthropic-native coding agent · first-class"],
      ["OpenClaw", "Open agent runtime · first-class"],
      ["Hermes", "Multi-agent orchestration · first-class"],
      ["Codex", "OpenAI-style coding workflow · supported"],
    ],
    models: [
      ["Claude 4.x / 4.6+", "Explicit breakpoints and prewarm path"],
      ["GPT 4+ / 5.x", "prompt_cache_key routing strategy"],
      ["DeepSeek V3+", "Deterministic byte-stable prefix"],
    ],
    frameworks: [
      ["vLLM", "Anchors · prewarm · probe / evict · partial fork-replace"],
      ["SGLang", "Anchors · prewarm · full fork-replace"],
    ],
  },
};

/* ───────── data: roadmap ───────── */
const ROADMAP = {
  zh: [
    ["协议正确性硬化", "把「缓存不可失效」从口号变成 CI 上的红绿灯。"],
    ["生产可靠性与可观测性", "让网关足以安全地跑在别人的生产流量上。"],
    ["接管调用链", "从提示重写器,升级为 Agent 的流量平面。"],
    ["上下文成为资产", "轨迹不再是日志——而是可分叉的代码。"],
  ],
  en: [
    ["Protocol correctness hardening", "Turn 'cache cannot be invalidated' from a slogan into a CI red/green light."],
    ["Production reliability & observability", "Make the gateway safe to leave on someone else's production traffic."],
    ["Take over the call chain", "Go from prompt rewriter to the agent's traffic plane."],
    ["Context becomes an asset", "Trajectories are no longer logs — they're forkable code."],
  ],
};

/* ───────── language ───────── */
let lang = localStorage.getItem("telos-lang") || "zh";

function t(key) {
  return (I18N[lang] && I18N[lang][key]) || (I18N.zh[key] || key);
}

function applyLang() {
  document.documentElement.lang = lang === "zh" ? "zh" : "en";

  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-html]").forEach((el) => {
    el.innerHTML = t(el.getAttribute("data-i18n-html"));
  });

  const toggle = document.getElementById("langToggle");
  toggle.classList.toggle("lang-zh", lang === "zh");
  toggle.classList.toggle("lang-en", lang === "en");

  renderCaps();
  renderMatrix();
  renderRoadmap();
  updateEstimator();
}

/* ───────── render: capabilities ───────── */
function renderCaps() {
  const grid = document.getElementById("capGrid");
  grid.innerHTML = "";
  CAPS[lang].forEach((c) => {
    const el = document.createElement("article");
    el.className = "cap-card";
    el.innerHTML = `<div class="cap-icon">${c.ic}</div><h3>${c.t}</h3><p>${c.d}</p>`;
    grid.appendChild(el);
  });
}

/* ───────── render: support matrix ───────── */
function renderMatrix() {
  const m = MATRIX[lang];
  const fill = (id, rows) => {
    const ul = document.getElementById(id);
    ul.innerHTML = "";
    rows.forEach(([name, note]) => {
      const li = document.createElement("li");
      li.innerHTML = `<b>${name}</b><span>${note}</span>`;
      ul.appendChild(li);
    });
  };
  fill("mxHarness", m.harness);
  fill("mxModels", m.models);
  fill("mxFrameworks", m.frameworks);
}

/* ───────── render: roadmap ───────── */
function renderRoadmap() {
  const ol = document.getElementById("timeline");
  ol.innerHTML = "";
  ROADMAP[lang].forEach(([title, desc]) => {
    const li = document.createElement("li");
    li.innerHTML = `<h3>${title}</h3><p>${desc}</p>`;
    ol.appendChild(li);
  });
}

/* ───────── estimator ───────── */
const SAVED_PCT = 0.923;
function updateEstimator() {
  const sessions = Number(document.getElementById("sessions").value);
  const baseline = Number(document.getElementById("baseline").value);
  const without = sessions * baseline;
  const saving = without * SAVED_PCT;
  const withT = without - saving;
  const fmt = (n) =>
    "$" + n.toLocaleString("en-US", { maximumFractionDigits: 2 });

  document.getElementById("sessionsOut").textContent =
    sessions.toLocaleString("en-US");
  document.getElementById("baselineOut").textContent = "$" + baseline.toFixed(2);
  document.getElementById("withoutOut").textContent = fmt(without);
  document.getElementById("withOut").textContent = fmt(withT);
  document.getElementById("savingOut").textContent = fmt(saving);
}

/* ───────── reveal on scroll ───────── */
function initReveal() {
  const els = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window)) {
    els.forEach((e) => e.classList.add("in"));
    return;
  }
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      });
    },
    { threshold: 0.14 }
  );
  els.forEach((e) => io.observe(e));
}

/* ───────── animated counters ───────── */
function initCounters() {
  const nodes = document.querySelectorAll(".count");
  if (!("IntersectionObserver" in window)) return;
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        const el = e.target;
        const target = parseFloat(el.dataset.count);
        const prefix = el.dataset.prefix || "";
        const suffix = el.dataset.suffix || "";
        const dur = 1100;
        const start = performance.now();
        const step = (now) => {
          const p = Math.min((now - start) / dur, 1);
          const eased = 1 - Math.pow(1 - p, 3);
          el.textContent = prefix + (target * eased).toFixed(1) + suffix;
          if (p < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
        io.unobserve(el);
      });
    },
    { threshold: 0.6 }
  );
  nodes.forEach((n) => io.observe(n));
}

/* ───────── nav scroll state ───────── */
function initNav() {
  const nav = document.querySelector(".nav");
  const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 12);
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });
}

/* ───────── lead form ───────── */
function initForm() {
  const form = document.getElementById("leadForm");
  const status = document.getElementById("formStatus");
  const btn = document.getElementById("leadSubmit");
  const emailRe = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    status.className = "form-status";
    status.textContent = "";

    const data = {
      name: form.name.value.trim(),
      company: form.company.value.trim(),
      email: form.email.value.trim(),
      volume: form.volume.value,
      message: form.message.value.trim(),
    };

    const bad = [];
    if (!data.name) bad.push(form.name);
    if (!data.company) bad.push(form.company);
    if (!emailRe.test(data.email)) bad.push(form.email);
    form.querySelectorAll(".invalid").forEach((e) => e.classList.remove("invalid"));
    if (bad.length) {
      bad.forEach((e) => e.classList.add("invalid"));
      status.className = "form-status err";
      status.textContent = t("contact.errFields");
      bad[0].focus();
      return;
    }

    btn.disabled = true;
    btn.querySelector("span").textContent = t("contact.sending");
    try {
      const res = await fetch("/api/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...data, lang, page: location.href }),
      });
      if (!res.ok) throw new Error("bad status " + res.status);
      status.className = "form-status ok";
      status.textContent = t("contact.ok");
      form.reset();
    } catch (err) {
      status.className = "form-status err";
      status.textContent = t("contact.errNet");
    } finally {
      btn.disabled = false;
      btn.querySelector("span").textContent = t("contact.submit");
    }
  });
}

/* ───────── boot ───────── */
document.getElementById("langToggle").addEventListener("click", () => {
  lang = lang === "zh" ? "en" : "zh";
  localStorage.setItem("telos-lang", lang);
  applyLang();
});

document.getElementById("sessions").addEventListener("input", updateEstimator);
document.getElementById("baseline").addEventListener("input", updateEstimator);

applyLang();
initReveal();
initCounters();
initNav();
initForm();
