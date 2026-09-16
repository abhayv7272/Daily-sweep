# NSE Daily Liquidity-Sweep Screener — Automated Daily Report

Har roz shaam **8:30 PM IST** tak yeh repo aapka
`NSE_Liquidity_Sweep_Screener_FIXED.ipynb` wala screener GitHub Actions par
headless chala kar poora report **abhayv7272@gmail.com** par email karta hai —
wahi results jo Colab mein "Run all" karne par aate hain, ek professional,
visually-polished HTML report ke saath, aur **har sweep ka marked candlestick
chart** ke saath.

---

## 1. Kya-kya milta hai roz ki email mein

| Cheez | Kahan |
|---|---|
| Poora HTML report (executive dark fintech theme: Hero header, KPIs, Trend Reversal Matrix, results table, setup spotlights, marked charts, rules, data-health) | email body mein inline + `daily_sweep_report_<date>.html` attachment mein |
| Results & Confluence Table — **Symbol, Close, Swept Low, Above %, Equal Lows, RSI Div, FVG Status, Vol x, Wick %, RSI, Grade, Score…** | report ke andar + `…_results_<date>.csv` attachment |
| **Why Trend Will Go UP 🚀 Checklist** — har setup ke liye vibrant glowing confluence badges (RSI Div, Equal Lows pool, FVG, Volume absorption, Hammer, EMAs) | report ke setup cards mein |
| Har setup ka high-res multi-panel chart — Candlesticks + Swept Low line + SL zone + **Bullish FVG shaded zone** + **Equal Lows** + **RSI Divergence ray & annotation panel** | report mein embedded + har chart alag `.png` attachment |
| Top-3 ka detailed trade-plan text (entry / SL / target 1 / target 2 (2R) / R:R) | report ke chart cards mein |
| Engine self-test (86 checks) + data-health report | report ke neeche + Actions logs mein |

Email subject: `NSE Daily Sweep · N setups · DD Mon YYYY`.
Agar uss din koi setup nahi banta, tab bhi email aati hai — "no setups" ke saath
data-health proof, taaki pata chale run healthy thi (silence = kuch toota, no-setups email = sab theek).

## 2. Ek-baar ka setup (5 minute) — ZAROORI

Schedule kaam kare, isse pehle repo mein 2 secrets daalne hain. Yeh wahi secrets
hain jo aapke `weekly-sweep` repo mein pehle se lage hue hain — lekin GitHub
repo-level secrets har repo mein alag daalne padte hain:

1. Repo kholo: **Settings → Secrets and variables → Actions → New repository secret**
2. **`MY_EMAIL`** = `abhayv7272@gmail.com`
3. **`MY_APP_PASSWORD`** = us Gmail ka **16-character App Password**
   - [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) par jao
   - (2-Step Verification on hona chahiye) → app ka naam do (jaise `daily-sweep`) → jo 16-letter password mile, wahi paste karo
   - yeh aapka normal Gmail password **nahin** hai — App Password hota hai
4. *(Optional)* **`REPORT_TO`** — agar report kisi aur address par chahiye. Khali chhoda to email khud `abhayv7272@gmail.com` par hi aati hai.

> Secrets daalne ke baad koi code change nahi chahiye — agli automatic (ya manual) run se email aane lagegi.

> **⚠️ Automatic daily run ke liye §7a wala cron-job.org setup ZAROORI hai.**
> GitHub ka apna internal cron is repo se hata diya gaya hai (duplicate email se
> bachne ke liye — §4), isliye roz ki email **sirf** cron-job.org ke ping se
> aati hai. Ping set nahi kiya to automatic email nahi aayegi; manual run
> (Actions → Run workflow) hamesha chalta rahega.

## 3. Test kaise karein (aaj hi)

**Actions → Daily Sweep Report → Run workflow** (right side) se turant test:

| Input | Kya karta hai |
|---|---|
| `limit = 60`, `send_email = false` | 3-minute smoke test: pehle 60 NSE symbols par poora pipeline + artifacts, email nahi bhejta |
| `limit = 0`, `send_email = true` | **full production run** (~5–10 min) — wahi cheez jo roz 8:30 PM IST hogi, email ke saath |

Har run ke artifacts (report HTML, CSV, saare chart PNGs) run page par bhi milte
hain — 60 din tak.

## 4. Schedule ka logic — ek hi automatic trigger

- **Email aapko ~20:30 IST (8:30 PM) par milni chahiye.** Poora pipeline (runner start + data fetch + charts + SMTP) ~6–7 minute ka hai, isliye trigger **20:23 IST** par fire hota hai. IST mein DST nahi hota — saal bhar waqt seedha rehta hai.
- 20:30 IST NSE close (15:30) ke 5 ghante baad hai — isliye scanned candle **confirmed close** hoti hai (notebook bhi 15:35 IST ke baad run karne ko kehta hai).
- **Automatic trigger sirf EK hai: cron-job.org ka `repository_dispatch` ping** (setup §7a). Workflow mein GitHub ka internal `schedule:` cron **jaan-boojh kar nahi hai** — dobara mat jodna.
- **Duplicate email ka reason yahi tha:** pehle GitHub cron (`53 14 * * *`) **aur** cron-job.org ka ping, dono ~20:23 IST par fire karte the ⇒ same din do runs ⇒ do emails. Ab automatic run ka single source of truth cron-job.org hai. Extra safety ke liye workflow mein ek **fail-open duplicate guard** bhi hai (§7b) — galti se double ping ho gaya to dusri email ruk jaati hai.
- **GitHub cron hatane ka doosra fayda — waqt reliable:** GitHub ka cron ek best-effort queue hai, timer nahi. Minute `:00` (top-of-hour) par duniya bhar ke saare repos ek saath queue hote hain aur free runners par trigger ghanton late ho sakta hai — 14–15 Sept 2026 ko 14:00 UTC ka cron **17:50/18:57 UTC** par start hua aur email **raat ~11:25 PM IST** par gayi thi. External ping par trigger turant milta hai; deri sirf runner pool ki hoti hai.
- **Der ho to email khud bataati hai:** automatic run par agar job 25+ minute late start hua, report ke header mein amber colour mein likha aata hai — *"run started Xh Ym late (planned ~20:30 IST · GitHub runner queue)"*. Ab email der se aaye to pata chal jayega ki run toota nahi, GitHub ka runner late mila.
- **Weekend / trading holiday:** NSE band hota hai, to "latest session" pichhle trading din ka rehta hai — report usi candle ko dobara bhejegi (candle date report mein saaf likhi hoti hai). Sirf Mon–Fri chahiye to cron-job.org job ko weekdays-only kar do — workflow mein koi change nahi chahiye.

## 5. "Colab jaisa hi result" kaise guaranteed hai

- `dlsweep/engine.py` aapki **`NSE_Liquidity_Sweep_Screener_FIXED.ipynb`** (main branch — wahi jo aap Colab mein chalate ho) ke code cells se **verbatim** copy hai (har block par cell number comment mein likha hai). Screening/ranking/scoring ka ek bhi number runner ke paas nahi aata — sab engine ka output hai.
- Sirf do **additive** robustness fixes hain (kisi detected setup/level/score par zero asar): agar yfinance kisi 1-symbol request par flat frame bheje to notebook use silently drop kar deta hai — engine use recover kar leta hai (download + backfill dono jagah).
- `config.py` notebook ke `SweepConfig` defaults ko verbatim mirror karta hai. Rules badalne ho to yahin badlo — notebook ke cell 3 mein bhi wahi change kar lena, dono same rahenge.
- Dependencies unpinned hain — Colab bhi latest install karta hai, Actions bhi latest; same versions ⇒ same behaviour.
- Har run mein **engine self-test** (notebook cell 12: synthetic candles par positive + 2 negative asserts) sabse pehle chalta hai; fail ho jaye to run abort ho jata hai, ghalat report kabhi nahi jaati.
- Intraday vs close: daily run market close ke kaafi baad (20:30 IST) hota hai, isliye 20:30-run aur aapka after-close Colab run same candle dekhte hain.

## 5b. QA — tests & deep diagnosis

- **`tests/test_offline.py`** — 55 offline checks (engine parsing, backfill aggregation, health gate, sweep accept/reject matrix, HTML/email edges). Har daily run inhe pehle chalata hai; ek bhi fail hua to pipeline abort ho jati hai — ghalat email kabhi nahi jaati. Local chalane ke liye: `python tests/test_offline.py`
- **Actions → Deep Diagnosis → Run workflow** — real-network probes: NSE archive reachability, Yahoo batch availability (fallback ~300 symbols, ranking params), 15-min backfill path, version pinning proof, aur optional Gmail SMTP login check (mail nahi bhejta). Kuch bhi "data aa nahi raha" jaisa lage to yahi pehla step hai.

## 6. Repo ka structure

```
NSE_Liquidity_Sweep_Screener_FIXED.ipynb   # aapki notebook (typo-repaired) — Colab wahi
dlsweep/engine.py                          # notebook ka engine, verbatim (source of truth)
run_sweep.py                               # headless runner (self-test → screen → charts → report → email)
report.py                                  # stunning HTML report builder (charts embedded)
emailer.py                                 # Gmail SMTP sender (inline HTML + full .html + saare .png)
config.py                                  # saare knobs (notebook SweepConfig + report settings)
.github/workflows/daily-sweep.yml          # daily trigger = cron-job.org ping (20:23 IST) + manual run; GitHub internal cron NAHI (duplicate email fix)
```

## 7. Troubleshooting

| Symptom | Matlab / fix |
|---|---|
| Run fail: `Missing secret(s): MY_EMAIL…` | §2 ka setup pending hai |
| Run fail: `SMTPAuthenticationError` | App Password galat/expired — naya banao, spaces mat rakho |
| Email nahi aayi par run green | Gmail spam folder check karo; `REPORT_TO` galat to nahi? |
| Report mein "NSE list fetch failed… fallback" line | GitHub ke IP se NSE archive block ho gaya — engine automatic ~300 liquid names par chal gaya (notebook ka hi designed behaviour) |
| Kisi din 0 setups | Normal hai — pattern roz nahi banta. Email tab bhi aayegi |
| Automatic email poori tarah aana band ho gayi | cron-job.org job dekho — active hai? last request `204/2xx` tha? token expire to nahi? (§7a). Repo mein ab GitHub ka koi internal cron **nahi** hai, isliye wahi ping ek lauta automatic trigger hai |
| Ek hi din do emails aayi thi | Purana issue, **fix ho chuka**: GitHub ka internal `schedule:` cron hata diya gaya — ab sirf cron-job.org ping automatic run karta hai, upar se duplicate guard (§7b) bhi hai |
| Run green hai par email nahi aayi, run summary mein "Duplicate-email guard" | Aaj already ek successful automated run ho chuka tha, isliye dusri email jaan-boojh kar roki gayi (artifacts us run par bhi bante hain). Email dobara chahiye to **Actions → Run workflow** (manual) chalao — manual run guard se bahar hai |
| Email 20:30 se kaafi der se aayi | Pehle report header ka amber note dekho — *"run started Xh Ym late (planned ~20:30 IST · GitHub runner queue)"* likha hai to ping apne waqt par pahuncha tha, deri GitHub ke shared runner pool ki thi. Roz ho raha ho to §7a ki job timing 10–15 minute pehle kar do |
| Local demo dekhna ho | `pip install -r requirements.txt && python run_sweep.py --demo --no-email` → `out/` mein sample report |

### §7a — Automatic daily trigger: cron-job.org (ZAROORI, ab optional nahi)

GitHub ka internal cron is repo se hata diya gaya hai (duplicate email ka reason wahi tha — §4). Ab roz ki automatic email **sirf** isi ping se aati hai. Workflow mein `repository_dispatch: daily-sweep` trigger pehle se laga hai; ping exact waqt par fire hota hai aur run turant start hota hai (GitHub cron queue ka wait nahi):

1. GitHub par ek **Personal Access Token** banao (Settings → Developer settings → Personal access tokens → fine-grained, sirf is repo ka **Contents: Read & Write** aur **Actions** access kaafi hai).
2. [cron-job.org](https://cron-job.org) (free) par daily job banao — **20:23 IST**, POST request:
   - URL: `https://api.github.com/repos/abhayv7272/Daily-sweep/dispatches`
   - Headers: `Authorization: Bearer <TOKEN>`, `Accept: application/vnd.github+json`
   - Body: `{"event_type": "daily-sweep"}`
3. Job save karke ek baar **"Run now"** se test karo — Actions tab mein `repository_dispatch` wala run turant dikhna chahiye aur ~6–7 minute mein email aa jani chahiye.
4. Sirf trading din chahiye to cron-job.org job mein Mon–Fri select kar lo — workflow mein koi change nahi chahiye.

> **Ping miss hua** (cron-job.org down, token expire, job disable) to us din automatic email nahi aayegi — GitHub ka koi backup cron jaan-boojh kar nahi rakha, kyunki wahi duplicate email ki jad tha. Us din ke liye **Actions → Daily Sweep Report → Run workflow** chala do.

### §7b — Duplicate-email guard

Workflow mein ek chhota **fail-open** guard hai: automatic run par wo dekhta hai ki **aaj (IST) isi branch ka koi automatic run already successfully complete ho chuka hai ya nahi**. Ho chuka hai ⇒ is run se email skip (report + artifacts phir bhi bante hain, aur run summary mein saaf likha aata hai). Do scheduler lag jayein, cron-job.org retry kar de, ya galti se double ping ho jaye — email ek hi baar jaati hai.

- Manual run (`workflow_dispatch`) guard ke **bahar** hai — wo email hamesha bhejta hai.
- Guard **fail-open** hai: GitHub API/`jq` check mein koi dikkat aayi to email normally chali jaati hai, guard kabhi legit email nahi rokta. Isi liye workflow ko `actions: read` permission chahiye.
- Guard hatana ho to `.github/workflows/daily-sweep.yml` ka *"Duplicate-email guard"* step delete kar do — baaki pipeline par koi asar nahi.

---
**⚠️ Disclaimer:** educational screening tool — investment advice nahi hai. Liquidity sweeps fail bhi hote hain; hamesha fixed-% risk aur stop ke saath trade karo.
