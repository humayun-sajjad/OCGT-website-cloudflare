/**
 * OCGT Worker — serves prerendered static site from dist/ via the [assets]
 * binding, and handles POST /api/contact (Brevo email forwarding).
 *
 * Required secrets (set via `wrangler secret put`):
 *   BREVO_API_KEY      — Brevo Transactional API key (xkeysib-...)
 *   CONTACT_TO_EMAIL   — recipient, e.g. info@ocgt.de
 *   CONTACT_FROM_EMAIL — verified Brevo sender, e.g. no-reply@ocgt.de
 *   CONTACT_FROM_NAME  — display name (optional, default "OCGT Website")
 *   TURNSTILE_SECRET_KEY — optional, enables Cloudflare Turnstile verification
 */

const MAX_LEN = 2000;
const DEFAULT_ORIGINS = ['https://ocgt.de', 'https://www.ocgt.de'];

function allowedOrigins(env) {
  const extra = (env.APP_BASE_URL || '').trim().replace(/\/+$/, '');
  return extra ? [...DEFAULT_ORIGINS, extra] : DEFAULT_ORIGINS;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === '/api/contact') {
      if (request.method === 'OPTIONS') {
        return new Response(null, {
          status: 204,
          headers: corsHeaders(request.headers.get('Origin') || '', env),
        });
      }
      if (request.method === 'POST') {
        return handleContact(request, env);
      }
      return new Response('Method Not Allowed', { status: 405 });
    }

    return env.ASSETS.fetch(request);
  },
};

async function handleContact(request, env) {
  const origin = request.headers.get('Origin') || '';
  const cors = corsHeaders(origin, env);

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'invalid_json' }, 400, cors);
  }

  const vorname = sanitize(body.vorname);
  const nachname = sanitize(body.nachname);
  const emailRaw = (body.email || '').trim();
  const email = sanitize(emailRaw);
  const firma = sanitize(body.firma);
  const leistung = sanitize(body.leistung);
  const nachricht = sanitize(body.nachricht);
  const subject = sanitize(body._subject) || 'Neue Anfrage — OCGT Website';

  if (!vorname || !nachname || !nachricht || !isEmail(emailRaw)) {
    return json({ error: 'validation_failed' }, 400, cors);
  }

  if (env.TURNSTILE_SECRET_KEY) {
    const token = (body.cfTurnstileToken || '').trim();
    if (!token) return json({ error: 'captcha_missing' }, 400, cors);
    try {
      const params = new URLSearchParams();
      params.append('secret', env.TURNSTILE_SECRET_KEY);
      params.append('response', token);
      const ip = request.headers.get('CF-Connecting-IP');
      if (ip) params.append('remoteip', ip);
      const verifyRes = await fetch(
        'https://challenges.cloudflare.com/turnstile/v0/siteverify',
        { method: 'POST', body: params }
      );
      const data = await verifyRes.json().catch(() => ({}));
      if (!data.success) return json({ error: 'captcha_failed' }, 403, cors);
    } catch {
      return json({ error: 'captcha_unavailable' }, 503, cors);
    }
  }

  const apiKey = env.BREVO_API_KEY;
  const toEmail = env.CONTACT_TO_EMAIL || 'info@ocgt.de';
  const fromEmail = env.CONTACT_FROM_EMAIL || 'no-reply@ocgt.de';
  const fromName = env.CONTACT_FROM_NAME || 'OCGT Website';

  if (!apiKey) return json({ error: 'server_misconfigured' }, 500, cors);

  const html = `
    <h2>${subject}</h2>
    <table cellpadding="6" style="font-family:Arial,sans-serif;font-size:14px;border-collapse:collapse">
      <tr><td><b>Vorname</b></td><td>${vorname}</td></tr>
      <tr><td><b>Nachname</b></td><td>${nachname}</td></tr>
      <tr><td><b>E-Mail</b></td><td><a href="mailto:${email}">${email}</a></td></tr>
      <tr><td><b>Unternehmen</b></td><td>${firma || '—'}</td></tr>
      <tr><td><b>Leistung</b></td><td>${leistung || '—'}</td></tr>
      <tr><td valign="top"><b>Nachricht</b></td><td>${nachricht.replace(/\n/g, '<br>')}</td></tr>
    </table>
    <hr><p style="font-size:12px;color:#666">Gesendet via ocgt.de — ${new Date().toISOString()}</p>
  `;

  const brevoRes = await fetch('https://api.brevo.com/v3/smtp/email', {
    method: 'POST',
    headers: {
      'api-key': apiKey,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({
      sender: { name: fromName, email: fromEmail },
      to: [{ email: toEmail }],
      replyTo: { email, name: `${vorname} ${nachname}` },
      subject,
      htmlContent: html,
    }),
  });

  if (!brevoRes.ok) {
    const detail = await brevoRes.text().catch(() => '');
    console.error('brevo_failed', brevoRes.status, detail);
    return json({ error: 'send_failed' }, 502, cors);
  }

  const isEN = /^en/i.test(sanitize(body._lang) || '') || /english|new enquiry/i.test(subject);
  const confirmSubject = isEN
    ? 'We received your message — OCGT'
    : 'Wir haben Ihre Nachricht erhalten — OCGT';
  const confirmHtml = isEN ? `
      <p>Hello ${vorname} ${nachname},</p>
      <p>Thank you for contacting OCGT. We have received your message and will get back to you within 1–2 business days.</p>
      <p>For reference, here is a copy of what you sent:</p>
      <blockquote style="border-left:3px solid #ccc;padding:0 12px;color:#555;font-family:Arial,sans-serif;font-size:14px">
        ${nachricht.replace(/\n/g, '<br>')}
      </blockquote>
      <p>Best regards,<br>OCGT — Octacon Geotechnik GmbH</p>
      <p style="font-size:12px;color:#666">This is an automated confirmation. Please do not reply to this email.</p>
    ` : `
      <p>Hallo ${vorname} ${nachname},</p>
      <p>vielen Dank für Ihre Nachricht. Wir haben Ihre Anfrage erhalten und melden uns innerhalb von 1–2 Werktagen bei Ihnen.</p>
      <p>Zur Übersicht hier eine Kopie Ihrer Nachricht:</p>
      <blockquote style="border-left:3px solid #ccc;padding:0 12px;color:#555;font-family:Arial,sans-serif;font-size:14px">
        ${nachricht.replace(/\n/g, '<br>')}
      </blockquote>
      <p>Mit freundlichen Grüßen,<br>OCGT — Octacon Geotechnik GmbH</p>
      <p style="font-size:12px;color:#666">Dies ist eine automatische Bestätigung. Bitte antworten Sie nicht auf diese E-Mail.</p>
    `;

  const confirmRes = await fetch('https://api.brevo.com/v3/smtp/email', {
    method: 'POST',
    headers: {
      'api-key': apiKey,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({
      sender: { name: fromName, email: fromEmail },
      to: [{ email, name: `${vorname} ${nachname}` }],
      replyTo: { email: toEmail, name: fromName },
      subject: confirmSubject,
      htmlContent: confirmHtml,
    }),
  });

  if (!confirmRes.ok) {
    const detail = await confirmRes.text().catch(() => '');
    console.error('brevo_confirm_failed', confirmRes.status, detail);
  }

  return json({ ok: true }, 200, cors);
}

function sanitize(v) {
  if (typeof v !== 'string') return '';
  return v
    .replace(/[<>"'&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#x27;', '&': '&amp;' }[c]))
    .trim()
    .slice(0, MAX_LEN);
}

function isEmail(s) {
  return typeof s === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(s);
}

function corsHeaders(origin, env) {
  const list = allowedOrigins(env);
  const allow = list.includes(origin) ? origin : list[0];
  return {
    'Access-Control-Allow-Origin': allow,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    Vary: 'Origin',
  };
}

function json(obj, status, extraHeaders) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
  });
}
