/**
 * TELOS — lead capture function
 *
 * POST /api/lead  { name, company, email, volume?, message?, lang?, page? }
 *
 * Sends a notification email via the Resend HTTP API.
 * The full lead is always console.log'd so nothing is lost even if email
 * delivery fails — recover it with `vercel logs`.
 *
 * Environment variables (set in the Vercel project — Settings → Environment):
 *   RESEND_API_KEY   required to actually send email
 *   LEAD_NOTIFY_TO   destination inbox for leads      (default: georgewang2011@163.com)
 *   LEAD_FROM        verified sender address          (default: onboarding@resend.dev)
 */

const escapeHtml = (s = "") =>
  String(s).replace(/[&<>"]/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
  }[c]));

const isEmail = (s) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(s || ""));

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ ok: false, error: "method_not_allowed" });
  }

  // Body is auto-parsed by Vercel for application/json; fall back to manual parse.
  let body = req.body;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      body = {};
    }
  }
  body = body || {};

  const name = String(body.name || "").trim().slice(0, 200);
  const company = String(body.company || "").trim().slice(0, 200);
  const email = String(body.email || "").trim().slice(0, 200);
  const volume = String(body.volume || "").trim().slice(0, 80);
  const message = String(body.message || "").trim().slice(0, 4000);
  const lang = String(body.lang || "").trim().slice(0, 8);
  const page = String(body.page || "").trim().slice(0, 500);

  if (!name || !company || !isEmail(email)) {
    return res.status(400).json({ ok: false, error: "invalid_fields" });
  }

  const lead = {
    receivedAt: new Date().toISOString(),
    name,
    company,
    email,
    volume,
    message,
    lang,
    page,
    ip: req.headers["x-forwarded-for"] || "",
  };

  // Always log — survives email failure, recoverable via `vercel logs`.
  console.log("TELOS_LEAD", JSON.stringify(lead));

  const apiKey = process.env.RESEND_API_KEY;
  const to = process.env.LEAD_NOTIFY_TO || "georgewang2011@163.com";
  const from = process.env.LEAD_FROM || "TELOS Site <onboarding@resend.dev>";

  if (!apiKey) {
    // Not configured yet — accept the lead so the form UX still works.
    console.warn("RESEND_API_KEY not set — lead logged only, no email sent.");
    return res.status(200).json({ ok: true, emailed: false });
  }

  const html = `
    <h2>New TELOS partnership lead</h2>
    <table cellpadding="6" style="border-collapse:collapse;font-family:sans-serif">
      <tr><td><b>Name</b></td><td>${escapeHtml(name)}</td></tr>
      <tr><td><b>Company</b></td><td>${escapeHtml(company)}</td></tr>
      <tr><td><b>Email</b></td><td>${escapeHtml(email)}</td></tr>
      <tr><td><b>Monthly calls</b></td><td>${escapeHtml(volume) || "—"}</td></tr>
      <tr><td><b>Message</b></td><td>${escapeHtml(message) || "—"}</td></tr>
      <tr><td><b>Lang / Page</b></td><td>${escapeHtml(lang)} · ${escapeHtml(page)}</td></tr>
      <tr><td><b>Received</b></td><td>${lead.receivedAt}</td></tr>
    </table>`;

  try {
    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        from,
        to: [to],
        reply_to: email,
        subject: `TELOS lead · ${name} @ ${company}`,
        html,
      }),
    });

    if (!r.ok) {
      const detail = await r.text();
      console.error("Resend send failed", r.status, detail);
      // Lead is logged; still return 200 so the visitor isn't blocked.
      return res.status(200).json({ ok: true, emailed: false });
    }

    return res.status(200).json({ ok: true, emailed: true });
  } catch (err) {
    console.error("Resend request error", err);
    return res.status(200).json({ ok: true, emailed: false });
  }
}
