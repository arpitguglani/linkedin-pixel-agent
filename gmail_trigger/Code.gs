/**
 * Gmail -> VM webhook trigger for LinkedIn message notifications.
 *
 * Runs inside YOUR Google account (free). Every 5 minutes it looks for new
 * LinkedIn message/InMail notification emails, sends a small HMAC-signed
 * summary (sender, subject, snippet — never the full email) to your VM, and
 * labels the email so it is processed once.
 *
 * Setup (see README): Project Settings -> Script properties:
 *   WEBHOOK_URL    = https://<your-domain>/webhook/gmail
 *   WEBHOOK_SECRET = <same value as WEBHOOK_SECRET in .env>
 * Then run `install` once and approve the permissions.
 */

var LABEL_NAME = 'AI_LinkedIn_Processed';
var QUERY =
  'from:(messages-noreply@linkedin.com OR inmail-hit-reply@linkedin.com OR ' +
  'hit-reply@linkedin.com) -label:' + LABEL_NAME + ' newer_than:1d';

function install() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkLinkedInEmails') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('checkLinkedInEmails').timeBased().everyMinutes(5).create();
  getOrCreateLabel_();
  Logger.log('Installed 5-minute trigger.');
}

function getOrCreateLabel_() {
  return GmailApp.getUserLabelByName(LABEL_NAME) || GmailApp.createLabel(LABEL_NAME);
}

/** "Jane Doe <messages-noreply@linkedin.com>" -> "Jane Doe" (strip "via LinkedIn"). */
function senderName_(from) {
  var name = String(from || '').replace(/<[^>]*>/g, '').replace(/"/g, '').trim();
  return name.replace(/\s+via LinkedIn$/i, '').trim();
}

function toHex_(bytes) {
  return bytes
    .map(function (b) {
      var v = (b < 0 ? b + 256 : b).toString(16);
      return v.length === 1 ? '0' + v : v;
    })
    .join('');
}

function sign_(secret, timestamp, body) {
  var raw = Utilities.computeHmacSha256Signature(timestamp + '.' + body, secret);
  return 'sha256=' + toHex_(raw);
}

function checkLinkedInEmails() {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('WEBHOOK_URL');
  var secret = props.getProperty('WEBHOOK_SECRET');
  if (!url || !secret) throw new Error('Set WEBHOOK_URL and WEBHOOK_SECRET script properties');

  var threads = GmailApp.search(QUERY, 0, 20);
  if (!threads.length) return;

  var events = [];
  threads.forEach(function (thread) {
    thread.getMessages().forEach(function (m) {
      events.push({
        message_id: m.getId(),
        from_name: senderName_(m.getFrom()),
        subject: m.getSubject(),
        snippet: m.getPlainBody().replace(/\s+/g, ' ').slice(0, 280),
        received_at: m.getDate().toISOString(),
      });
    });
  });

  var body = JSON.stringify({ events: events });
  var ts = String(Math.floor(Date.now() / 1000));
  var resp = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: body,
    headers: { 'X-Timestamp': ts, 'X-Signature-256': sign_(secret, ts, body) },
    muteHttpExceptions: true,
  });

  var code = resp.getResponseCode();
  if (code >= 200 && code < 300) {
    var label = getOrCreateLabel_();
    threads.forEach(function (t) {
      t.addLabel(label);
    });
  } else {
    // Leave unlabeled so the next run retries (server dedupes by message_id).
    Logger.log('Webhook failed: HTTP ' + code);
  }
}
