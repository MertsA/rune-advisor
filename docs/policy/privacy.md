---
page: privacy
revision: Version of 24 November 2026. The policy describes what the service does and what it may come to do; where the two differ today, it says so.
---

# Privacy policy

## 1. Who is responsible

Rune Advisor is operated independently by an individual developer in Washington State, United States, not a
company. That person is responsible for the data described here ("we" and "the operator" below).

Contact for anything in this policy: <privacy@runeadvisor.com>. General questions: <support@runeadvisor.com>.

## 2. What is collected, and from where

### From the in-game plugin, if you install it and give it a token

Your character's display name and data about your character and your play that the plugin can read from the
game. Today that is its game state: skill experience, quest and achievement progress, currencies, and the
contents of your own item storage. What the current version sends is listed on [what the plugin
sends](/docs/data/). The plugin sends nothing until you paste a token into it.

The game keeps a plugin's settings for the computer, not for one login. A token saved in the plugin
therefore sends every character played in that game client on that computer, yours or anyone else's,
until the token is removed from the settings or revoked. On a computer you share, tell the others who play
there, or remove the token when you finish.

Later versions may collect more, for example the messages the game itself writes into your chat box (such as
"You catch a fish"), your character's position in the game, or a record of a play session: where your
character went, what it did, and the experience and items that brought, so the planner can work from what an
activity really yields for you. That list is updated before any version that sends more is released, and every
plugin version is reviewed by Jagex.

The server keeps the latest state the plugin sent. It may also come to keep the changes the plugin sends as a
history of your character's progress; that needs nothing more from the plugin than it sends already.

**The game's own definitions.** After a game update, the plugin may also send the game's quest and
achievement definitions that changed: their names, requirements and descriptions. These describe the game,
are the same for every player, and say nothing about you or your character. They are used for every player:
to keep the service working after an update, and to develop and improve the planner, of which they may
become a part. This does not depend on whether you allow analysis (section 3), and they are kept after the
account that sent them is deleted. Definitions sent by one account are used for other players only once
they have been checked, by the operator or because enough other accounts sent exactly the same.

**Chat between players is never collected.** Reading the game's own messages needs the same plugin permission
as reading chat, because the game delivers both through the chat box; if a later version asks for that
permission, messages written to or by any player, you included, are not sent or stored. The plugin also does
not collect other players' data, your Jagex account's login details, or anything about you outside the game.

### From Jagex's public services

While your plugin is connected, the service may use your character's name to ask Jagex's public services (such
as the hiscores and the Adventurer's Log) for the information about your character that you have chosen to
make public in your game settings, and keep it with your character's data. What you have set to private in the
game is not available to us this way. The service does not do this yet.

### When you use the web planner

The web planner runs in your browser. It reads your characters' data from the API with your account's planner
token, the same way a planner on your computer does, and does its planning on your device: what it works out
is not sent to us. It keeps the name of the character you last opened in your browser's storage and its token
for as long as the tab is open, and sends the token with each request to the API.

### When you make an account

An account is a random key: there is no email address, name or password. We store a hash of the key, a random
account id, and when the account was made and last used. For each token: a hash of it, its first six
characters, the label you typed, what it may do, when it was made and last used, and which of your characters
it has posted for, so that you can tell your tokens apart. Tokens made on the account page are worked out in
your browser from your account key, so that the page can show them to you again later; we receive each token
only as any other, and store only a hash of it. For each character: whether, and since when, you
allow analysis (section 3). The internet (IP) address
an account was made from may be stored, to limit how many accounts can be made from one place.

Making an account runs a check that you are not a bot, provided by Cloudflare (Turnstile). For it your
browser contacts Cloudflare, which sees your internet address and technical details of your browser, and we
send Cloudflare the address your request came from so that it can confirm the check. Cloudflare uses this to
tell people from bots and to improve that detection, not to identify you. It happens only when you make an
account: not when you open one, and never from the plugin.

### Settings you save with a token

A planner can save its settings with its token, so that they follow you to another browser or computer. They
are encrypted on your device, with a key that comes from the token and that we never receive, before they are
sent. We store them encrypted, with when they were last changed and how large they are, and cannot read them.
Settings can hold whatever you type into them, character names included; they are deleted with the token or
the account.

### If you send a report from the planner

The planner may let you report a problem with its advice (a planned feature, not built yet). A report holds
what you write and which account and character it is about. Only if you choose to allow it with that report,
the operator may look at that character's stored data, as it was when you sent the report, to investigate it.
Nothing from your data is published: fixes are tested with made-up data, and public notes refer to a report by
its number only. What you write in a report is kept (see section 6), so leave out anything personal.

### When you write to us

Your email address and whatever you write. To act on a request about an account we may ask you to show that it
is yours, for example with one of its tokens.

### From every request to the site or the API

The server logs the internet (IP) address the request came from, the time, the request, the response status
and size, the referring page and the browser or client name. Keys and tokens are not logged, and neither is
the content of what the plugin posts.

### Cookies and usage statistics

The site sets no cookies today. The [account page](/account/) keeps your key in the page's memory only; the web
planner keeps what section 2 says above in your browser's storage. Large downloads, such as the web planner's
program files and the planner's data updates, are delivered through Cloudflare's network: for them your browser
or the planner connects to Cloudflare, which sees your internet address and technical details of the request,
and serves files that are the same for everyone. Apart from that and the bot check when you make an account
(above), the site loads nothing from other companies' servers. The API is not reached through Cloudflare. We may measure how the site and the service are used (for example
unique visitors, or how many players are connected at once), and may use cookies or similar means for that.
Such statistics are kept in a form that does not identify you. There is no advertising.

## 3. What it is used for

To do what the tool is for: to let your planner, or a planner you gave a token to, read your character's data
and plan from it, which can include combining what the plugin sent with what Jagex's public services say about
the same character; to keep the service running and secure; to understand in aggregate how the service is used
and improve it; and to carry out deletions. It is not sold and not shared for advertising.

### Analysis to improve the planner, only if you allow it

You can allow a character's data to be analysed to improve the planner for everyone: for example to learn
from many players' data how much experience an activity gives, how fast a training method really is, or when
the game resets a daily activity. This is optional and off unless you turn it on. The planner works the same
for you either way, and allowing it does not make the plugin send anything more or the server keep anything
for longer.

The choice is made for each character. You make it, and can change it at any time, in the plugin's settings
or on the [account page](/account/). It covers that character's data whenever it was sent: once you allow
it, data already stored may be analysed; once you stop allowing it, no later analysis uses any of it, old or
new.

An analysis looks at play, not at players. It reads the data without character names or account details,
and only from the characters that allow analysis at that moment. It keeps nothing but its results: the data
it read is not kept anywhere outside the service once the analysis is done. Where an analysis runs may change
over time: on the service's servers, or on other equipment the operator uses for it, including the
operator's own. Wherever it runs, it is in the United States, only the operator and the service providers in
section 4 have access, and what this section says still holds.

**The results are public.** A result is a statement about the game: what an activity gives, how a mechanic
works, how fast a method is, or a way of playing that the data showed to work. Results go into the planner
and its public source code, and they stay there after you stop allowing analysis or delete your data. A
result never names or describes a player: no character name, no account, nothing about what a character
owns or has achieved, and nothing about when or how much anyone plays. It does not say whose data it came
from or how many characters'.

A result is usually drawn from many characters, but it can come from a few, or from one. If you allow
analysis and you play in a way nobody else does, that way of playing may end up described in the planner for
everyone, without saying where it came from. If you would rather keep it to yourself, do not allow analysis
for that character.

An analysis may also produce **statistics about players as a group**, such as the share of characters above
some total level. These too are drawn only from characters that allow analysis, and may be published. They
are published only for groups large enough that nothing can be learned from them about any one player, and
never to single out a small group or one whose members could be recognised, such as the owners of a rare
item. Of a group too small for that, nothing is published beyond how many characters it holds.
Simple counts of how the service is used, such as how many characters allow analysis, are usage statistics
(section 2) and do not depend on this choice.

Where UK or EU data protection law applies, these are the legal grounds we rely on:

- **Providing the service you asked for**, under the [terms](/terms/) you accept by making an account and
  giving your plugin a token: storing what your plugin sends, handing it to your planner, keeping your account
  and tokens, keeping the settings you save with a token, looking up your character's public data when you have turned that on, and handling a report you
  send from the planner.
- **Our legitimate interests** in keeping the service secure and working and in knowing how it is used:
  server logs, limits on how quickly accounts can be made and requests sent, the bot check when an account is
  made, delivering large downloads through a content delivery network, backups, records of deletions, and usage statistics that do not identify you. You can object to this
  (section 8).
- **Legal obligations:** answering a request you make about your data, and keeping a record that we did.
- **Your consent:** analysing what your plugin sends to improve the planner (above). Nothing else relies on
  your consent, and withdrawing it is as easy as giving it. If a later feature needs consent, it will ask
  first and you will be able to withdraw it. You do not have to give us any data: without the plugin's data the planner
works from public sources only. Nothing here is used to make decisions about you automatically.

## 4. Who can see it

- **You**, and anyone you give a token to (for example a group mate, with a `read` token limited to the
  characters you choose).
- **The operator**, who runs the service.
- **Service providers** the operator uses to run the service, such as the company that hosts the server, and
  Cloudflare for the bot check and for delivering large downloads, only as far as running the service requires.

Game data read through the plugin belongs to Jagex Ltd under its terms for plugins; it is used only for this
tool's function and is deleted when you ask.

## 5. Where it is

On servers in the **United States**, where the operator and the hosting company are. Your plugin and your
browser send data there directly. An analysis you allowed (section 3) may read the data
from elsewhere in the United States, on other equipment the operator uses for it.

Downloads delivered through Cloudflare (section 2) are handled at a Cloudflare location near you, which may be
outside the United States; they are files that are the same for everyone, and what Cloudflare sees of your
request is your internet address and technical details of it.

Backups may also be kept with a storage provider in the United States or the European Union. They are
encrypted before they leave the server, and the key that opens them is not kept with them.

If you are in the UK or the EU, this means your data is held in a country that neither has found to protect
personal data to the same standard as its own law. Their arrangements with the United States cover only
companies that have joined the Data Privacy Framework, which this service, run by an individual, has not.
Authorities in the United States could require access to data held there under US law.

What protects your data is therefore what this policy describes, not a law of the country it is in: little is
collected, there is no name or email address with it, it is encrypted on the way, backups are encrypted, and
you can delete it at any time. Game data that reaches us through the plugin is also covered by Jagex's terms
for plugin developers, which include standard data protection clauses for developers outside the UK and the
EU.

## 6. How long it is kept

| What | Kept |
|---|---|
| A character's data | Until you delete the character or the account, or the account expires. If the server keeps a history of a character's progress, entries older than one year are dropped. |
| An account and its tokens | Until you delete it, or **one year after it was last used** (a plugin posting or a planner reading), when it is deleted automatically with all its characters. |
| Settings saved with a token | Until you change or delete them, the token is revoked, or the account is deleted or expires. They are encrypted, and we cannot read them. |
| Records of deletions | When you delete an account or a character, or revoke a token, a record that it happened is kept for about 90 days: random ids and hashes, never a character name or a token. It lets a plugin still holding a revoked token be told to stop, and makes sure a restore from a backup cannot bring deleted data back. These records are identifiers of our own, not data from the game. |
| Reports sent from the planner | The text of a report is kept, because it stays useful for later work on the planner, until you ask us to delete it: write to us with the report's number. Its link to your account is removed when the account or the character is deleted. A report holds no copy of character data: it points at the character's stored data, which is kept and deleted as the first row says. |
| Emails you send us | Kept for as long as it takes to deal with your request and to be able to show afterwards that we did. If you ask for a copy of your data, we send it and do not keep the copy with the correspondence. |
| Server logs | Up to 30 days with the internet address. After that the address is removed; what is left of a log line no longer identifies anyone and, like statistics drawn from the logs, may be kept for longer. |
| Backups | Backups are encrypted. Backups that contain character data are kept for up to 14 days, so a deleted character's data is gone from every copy within that time. Backups of account records only (the hashes and ids above, and settings saved with tokens, still encrypted; no character data from the plugin) are kept for up to 90 days. Deleted data is deleted again if a backup is ever restored. One exception: a restore can only repeat the deletions it has a record of. If the record of your deletion is itself lost before any backup has captured it, restoring a backup would bring the deleted account or character back, and you would need to delete it again. |

## 7. How it is protected

- All traffic to the site and the API is encrypted (HTTPS).
- Keys and tokens are stored only as hashes. A token for the plugin cannot read anything back.
- Settings saved with a token are encrypted on your device with a key we never receive.
- If we find that a token may have been exposed, we can retire it; the account page then gives it a new value
  the next time you open it, and settings saved with it carry over.
- Backups are encrypted.
- Access to the server is limited to the operator and the service providers in section 4, and stored character
  data is accessed only when running the service requires it.

## 8. Your choices

- **See your data.** A `read` token returns everything stored for a character; the [account
  page](/account/) shows what is stored about the account and its tokens. Settings saved with a token are
  shown by the planner that uses it; we cannot read them.
- **Delete it.** On the [account page](/account/), with your key: one character, or the whole account. This
  takes effect at once on the live service; see section 6 for what remains for a time. Revoking a token
  deletes the settings saved with it.
- **Allow or stop allowing analysis**, character by character. In the plugin's settings, or on the
  [account page](/account/) with your key or the plugin's token. It is off unless you turn it on (section 3).
- **Stop sending.** Remove the token from the plugin's settings, revoke it, or uninstall the plugin.
- **If you lost your key.** The token in your plugin still works for one thing: paste it into the [account
  page](/account/) in place of the key and you can see and delete what that plugin posted (the plugin's
  settings can show its token again). Without the key or a token we cannot tell which account is yours; stop
  the plugin and the account is deleted after a year without use.

### Your rights

Where UK or EU data protection law applies, you have the right to:

- **see** the data held about you and get a copy of it (a `read` token returns a character's data in a form
  another program can read; the account page shows the rest; or ask us);
- have it **corrected** (a character's data is whatever its plugin last sent, so it corrects itself at the
  next post; tell us if something else is wrong);
- have it **deleted**;
- **object** to what we do on the ground of our legitimate interests (section 3), or ask us to **restrict**
  it while a question about it is settled;
- **complain** (below).

Most of this you can do yourself on the [account page](/account/). For anything else write to
<privacy@runeadvisor.com>. We answer within one month; the law allows up to two more for a difficult request,
and we would tell you within the first month if we needed them.

**Showing that the data is yours.** An account has no name or email address, so we can act on an account or a
character only when you show that it is yours, with the account key or one of its tokens (the plugin's
settings can show its token again). A character's name alone is not proof: anyone can type one, and names
change hands in the game. If you can show neither, we are usually unable to tell which data is yours and so
unable to act on the request; the account is deleted a year after it was last used in any case. Server logs
hold an internet address and a time, and we cannot normally tell whose a line is.

**Complaints.** Tell us first at <privacy@runeadvisor.com>: we acknowledge a complaint within 30 days and
answer it without undue delay. You can also complain to the data protection authority where you live: in the
UK the [Information Commissioner's Office](https://ico.org.uk/make-a-complaint/data-protection-complaints/),
in the EU [your national authority](https://www.edpb.europa.eu/about-edpb/about-edpb/members_en).

## 9. Children

The service is not directed at children. You must be at least 13 years old to use it, and if you are under
18 you need your parent's or guardian's permission to agree to the [terms](/terms/), and their permission
before you allow analysis (section 3). We do not ask for anyone's age and do not knowingly
collect data from a child under 13; a parent or guardian who believes we have can write to
<privacy@runeadvisor.com> and we will delete it.

## 10. Changes

Changes to this policy are published on this page with their date. The plugin cannot send more than [what is
listed](/docs/data/) without a new version, which Jagex reviews.
