# NSE Daily Liquidity-Sweep Screener — Automated Daily Report

Har roz shaam **7:30 PM IST** ko yeh repo aapka
`NSE_Liquidity_Sweep_Screener_FIXED.ipynb` wala screener GitHub Actions par
headless chala kar poora report **abhayv7272@gmail.com** par email karta hai —
wahi results jo Colab mein "Run all" karne par aate hain, ek professional,
visually-polished HTML report ke saath, aur **har sweep ka marked candlestick
chart** ke saath.

---

## 1. Kya-kya milta hai roz ki email mein

| Cheez | Kahan |
|---|---|
| Poora HTML report (dark fintech theme: header, KPIs, results table, saare marked charts, rules, data-health) | email body mein inline + `daily_sweep_report_<date>.html` attachment mein |
| Results table — **bilkul wahi jo Colab `display()` dikhata hai** (Symbol, Swept Low, Depth %, Wick %, Vol x, RSI, Score…) | report ke andar + `…_results_<date>.csv` attachment |
| Har setup ka chart — swept swing-low dashed line + SL zone + `SWEEP ✓` marker (notebook ka `plot_setup` code) | report mein embedded + har chart alag `.png` attachment |
| Top-3 ka detailed trade-plan text (entry / SL / target / R:R) — notebook cell 15 jaisa | report ke chart cards mein |
| Engine self-test + data-health report | report ke neeche + Actions logs mein |

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

> Secrets daalne ke baad koi code change nahi chahiye — agli scheduled (ya manual) run se email aane lagegi.

## 3. Test kaise karein (aaj hi)

**Actions → Daily Sweep Report → Run workflow** (right side) se turant test:

| Input | Kya karta hai |
|---|---|
| `limit = 60`, `send_email = false` | 3-minute smoke test: pehle 60 NSE symbols par poora pipeline + artifacts, email nahi bhejta |
| `limit = 0`, `send_email = true` | **full production run** (~5–10 min) — wahi cheez jo roz 7:30 PM hogi, email ke saath |

Har run ke artifacts (report HTML, CSV, saare chart PNGs) run page par bhi milte
hain — 60 din tak.

## 4. Schedule ka logic

- Cron `0 14 * * *` = **14:00 UTC = 19:30 IST**, har din. IST mein DST nahi hota, GitHub UTC mein chalta hai — isliye saal bhar exact 19:30 IST.
- 19:30 IST NSE close (15:30) ke 4 ghante baad hai — isliye scanned candle **confirmed close** hoti hai (notebook bhi 15:35 IST ke baad run karne ko kehta hai).
- **Weekend / trading holiday:** NSE band hota hai, to "latest session" pichhle trading din ka rehta hai — report usi candle ko dobara bhejegi (candle date report mein saaf likhi hoti hai). Agar sirf Mon–Fri chahiye to workflow mein cron ko `"0 14 * * 1-5"` kar do.
- GitHub kabhi-kabhi shared-load par scheduled runs ko kuch minute der se start karta hai — normal hai.

## 5. "Colab jaisa hi result" kaise guaranteed hai

- `dlsweep/engine.py` aapki **`NSE_Liquidity_Sweep_Screener_FIXED.ipynb`** (main branch — wahi jo aap Colab mein chalate ho) ke code cells se **verbatim** copy hai (har block par cell number comment mein likha hai). Screening/ranking/scoring ka ek bhi number runner ke paas nahi aata — sab engine ka output hai.
- Sirf do **additive** robustness fixes hain (kisi detected setup/level/score par zero asar): agar yfinance kisi 1-symbol request par flat frame bheje to notebook use silently drop kar deta hai — engine use recover kar leta hai (download + backfill dono jagah).
- `config.py` notebook ke `SweepConfig` defaults ko verbatim mirror karta hai. Rules badalne ho to yahin badlo — notebook ke cell 3 mein bhi wahi change kar lena, dono same rahenge.
- Dependencies unpinned hain — Colab bhi latest install karta hai, Actions bhi latest; same versions ⇒ same behaviour.
- Har run mein **engine self-test** (notebook cell 12: synthetic candles par positive + 2 negative asserts) sabse pehle chalta hai; fail ho jaye to run abort ho jata hai, ghalat report kabhi nahi jaati.
- Intraday vs close: schedule close ke baad hai, isliye 19:30-run aur aapka after-close Colab run same candle dekhte hain.

## 5b. QA — tests & deep diagnosis

- **`tests/test_offline.py`** — 62 offline checks (engine parsing, backfill aggregation, health gate, sweep accept/reject matrix, HTML/email edges, aur IST email timestamp). Har daily run inhe pehle chalata hai; ek bhi fail hua to pipeline abort ho jati hai — ghalat email kabhi nahi jaati. Local chalane ke liye: `python tests/test_offline.py`
- **Actions → Deep Diagnosis → Run workflow** — real-network probes: NSE archive reachability, Yahoo batch availability (fallback ~300 symbols, ranking params), 15-min backfill path, version pinning proof, aur optional Gmail SMTP login check (mail nahi bhejta). Kuch bhi "data aa nahi raha" jaisa lage to yahi pehla step hai.

## 6. Repo ka structure

```
NSE_Liquidity_Sweep_Screener_FIXED.ipynb   # aapki notebook (typo-repaired) — Colab wahi
dlsweep/engine.py                          # notebook ka engine, verbatim (source of truth)
run_sweep.py                               # headless runner (self-test → screen → charts → report → email)
report.py                                  # stunning HTML report builder (charts embedded)
emailer.py                                 # Gmail SMTP sender (inline HTML + full .html + saare .png)
config.py                                  # saare knobs (notebook SweepConfig + report settings)
.github/workflows/daily-sweep.yml          # 19:30 IST daily schedule + manual triggers
```

## 7. Troubleshooting

| Symptom | Matlab / fix |
|---|---|
| Run fail: `Missing secret(s): MY_EMAIL…` | §2 ka setup pending hai |
| Run fail: `SMTPAuthenticationError` | App Password galat/expired — naya banao, spaces mat rakho |
| Email nahi aayi par run green | Gmail spam folder check karo; `REPORT_TO` galat to nahi? |
| Report mein "NSE list fetch failed… fallback" line | GitHub ke IP se NSE archive block ho gaya — engine automatic ~300 liquid names par chal gaya (notebook ka hi designed behaviour) |
| Kisi din 0 setups | Normal hai — pattern roz nahi banta. Email tab bhi aayegi |
| Local demo dekhna ho | `pip install -r requirements.txt && python run_sweep.py --demo --no-email` → `out/` mein sample report |

---
**⚠️ Disclaimer:** educational screening tool — investment advice nahi hai. Liquidity sweeps fail bhi hote hain; hamesha fixed-% risk aur stop ke saath trade karo.
