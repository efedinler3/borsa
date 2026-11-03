"""Rule-based category tagging, financial-lexicon sentiment and 0..100 importance (no ML, deterministic)."""
from __future__ import annotations

import math
import re
import threading

# ---------------------------------------------------------------- categories
# weight = nominal importance contribution of a clear (title) match; bias = typical price direction.
CATEGORIES: dict[str, dict] = {
    "earnings":      {"label": "Bilanço",                   "bias": 0,  "weight": 26, "color": "#60a5fa"},
    "guidance_up":   {"label": "Beklenti artırımı",         "bias": 1,  "weight": 32, "color": "#22c55e"},
    "guidance_down": {"label": "Beklenti indirimi",         "bias": -1, "weight": 32, "color": "#ef4444"},
    "analyst_up":    {"label": "Analist: not artırımı",     "bias": 1,  "weight": 20, "color": "#4ade80"},
    "analyst_down":  {"label": "Analist: not indirimi",     "bias": -1, "weight": 20, "color": "#f87171"},
    "mna":           {"label": "Birleşme / satın alma",     "bias": 1,  "weight": 32, "color": "#a78bfa"},
    "fda":           {"label": "FDA kararı",                "bias": 0,  "weight": 34, "color": "#2dd4bf"},
    "clinical":      {"label": "Klinik çalışma",            "bias": 0,  "weight": 26, "color": "#5eead4"},
    "offering":      {"label": "Hisse arzı / sulandırma",   "bias": -1, "weight": 24, "color": "#fb923c"},
    "buyback":       {"label": "Hisse geri alımı",          "bias": 1,  "weight": 18, "color": "#34d399"},
    "dividend":      {"label": "Temettü",                   "bias": 0,  "weight": 10, "color": "#facc15"},
    "insider":       {"label": "İçeriden işlem",            "bias": 0,  "weight": 12, "color": "#c084fc"},
    "contract":      {"label": "Sözleşme / anlaşma",        "bias": 1,  "weight": 18, "color": "#38bdf8"},
    "lawsuit":       {"label": "Dava / soruşturma",         "bias": -1, "weight": 16, "color": "#f97316"},
    "bankruptcy":    {"label": "İflas / konkordato",        "bias": -1, "weight": 36, "color": "#dc2626"},
    "layoffs":       {"label": "İşten çıkarma",             "bias": 0,  "weight": 14, "color": "#fca5a5"},
    "macro":         {"label": "Makro veri",                "bias": 0,  "weight": 26, "color": "#94a3b8"},
    "fed":           {"label": "Fed / faiz",                "bias": 0,  "weight": 30, "color": "#eab308"},
    "ipo":           {"label": "Halka arz",                 "bias": 0,  "weight": 20, "color": "#818cf8"},
    "halt":          {"label": "İşlem durdurma",            "bias": 0,  "weight": 30, "color": "#f43f5e"},
    "short_report":  {"label": "Açığa satış raporu",        "bias": -1, "weight": 30, "color": "#b91c1c"},
    "accounting":    {"label": "Muhasebe / denetim sorunu", "bias": -1, "weight": 28, "color": "#be123c"},
    "split":         {"label": "Hisse bölünmesi",           "bias": 1,  "weight": 16, "color": "#67e8f9"},
    "reverse_split": {"label": "Ters bölünme",              "bias": -1, "weight": 18, "color": "#fdba74"},
    "delisting":     {"label": "Kotasyon / borsadan çıkma", "bias": -1, "weight": 20, "color": "#e11d48"},
    "filing":        {"label": "SEC bildirimi",             "bias": 0,  "weight": 8,  "color": "#64748b"},
    "product":       {"label": "Ürün / lansman",            "bias": 0,  "weight": 10, "color": "#93c5fd"},
    "crypto":        {"label": "Kripto",                    "bias": 0,  "weight": 12, "color": "#f59e0b"},
    "geopolitics":   {"label": "Jeopolitik / ticaret",      "bias": 0,  "weight": 26, "color": "#e879f9"},
    "management":    {"label": "Yönetim değişikliği",       "bias": 0,  "weight": 18, "color": "#d8b4fe"},
    "activist":      {"label": "Aktivist / büyük pay",      "bias": 1,  "weight": 22, "color": "#86efac"},
    "commodities":   {"label": "Emtia (petrol, altın)",     "bias": 0,  "weight": 14, "color": "#d97706"},
    "index_change":  {"label": "Endeks değişikliği",        "bias": 1,  "weight": 24, "color": "#a3e635"},
    "market":        {"label": "Piyasa geneli",             "bias": 0,  "weight": 16, "color": "#cbd5e1"},
    "price_move":    {"label": "Sert fiyat hareketi",       "bias": 0,  "weight": 14, "color": "#fde047"},
    "ai":            {"label": "Yapay zekâ",                "bias": 0,  "weight": 5,  "color": "#7dd3fc"},
    "opinion":       {"label": "Yorum / liste",             "bias": 0,  "weight": 0,  "color": "#6b7280"},
}

SENTIMENTS = {"pos": "Olumlu", "neg": "Olumsuz", "neu": "Nötr"}

_UPV = r"(?:raises?|raised|raising|lifts?|lifted|boosts?|boosted|increases?|increased|ups|upped|hikes?|hiked)"
_DNV = (r"(?:cuts?|cutting|lowers?|lowered|lowering|slashes?|slashed|reduces?|reduced|trims?|trimmed|"
        r"decreases?|decreased|decreasing|withdraws?|withdrew|pulls?|pulled|suspends?|suspended)")
_GQ = (r"(?:(?:its|the|full[- ]year|annual|fiscal|fy ?\d{0,4}|20\d\d|quarterly|q[1-4]|revenue|sales|profit|earnings|"
       r"eps|ebitda|margin|growth|year|financial|long[- ]term|\d{4}|first[- ]half|second[- ]half|h[12])[\s-]+){0,4}")
_GW = r"(?:guidance|outlook|forecasts?|projections?|view)\b"
_EST = r"(?:estimates|expectations|forecasts|consensus|views|projections)"
_ACTIVISTS = (r"(?:elliott management|starboard value|icahn|trian|pershing square|ackman|jana partners|third point|"
              r"valueact|ancora|mantle ridge)")
_ACTIVIST_ACT = r"(?:stakes?|letter|board|proxy|pushes|push|urges?|nominat\w*|campaign|seats?)\b"
_FEDCTX = (r"(?:rates?|policy|chair\w*|meeting|officials?|minutes|governors?|powell|cuts?|hikes?|decision|independence|"
           r"easing|tightening|inflation|says|said|signals?|warns?|speakers?|board|nominee|pause[sd]?|holds?|bank)")
# Bare "upgrade" / "downgrade" count as analyst actions only next to a rating, a brokerage or a price target.
_ANALYST_UPGRADE = (r"\bupgrade[sd]? (?:\w+ ){0,3}(?:to|by) (?:buy|outperform|overweight|neutral|hold|equal[- ]weight|"
                    r"market perform|sector perform|accumulate|add)\b|\bupgrades? (?:\w+ ){0,3}(?:rating|stock|shares|coverage)\b|"
                    r"\b(?:analysts?|brokerages?|brokers?|strategists?|research|price targets?)\b(?:\W+\w+){0,6}?\W+upgrad\w*\b|"
                    r"\bupgrad\w*\b(?:\W+\w+){0,6}?\W+(?:analysts?|brokerages?|brokers?|price targets?|ratings?)\b")
_ANALYST_DOWNGRADE = (r"\bdowngrade[sd]? (?:\w+ ){0,3}(?:to|by) (?:sell|underperform|underweight|neutral|hold|equal[- ]weight|"
                      r"market perform|sector perform|reduce)\b|\bdowngrades? (?:\w+ ){0,3}(?:rating|stock|shares|coverage)\b|"
                      r"\b(?:analysts?|brokerages?|brokers?|strategists?|research|price targets?)\b(?:\W+\w+){0,6}?\W+downgrad\w*\b|"
                      r"\bdowngrad\w*\b(?:\W+\w+){0,6}?\W+(?:analysts?|brokerages?|brokers?|price targets?|ratings?)\b")
# Title-level analyst vocabulary: needed before an analyst_* category may come from the summary alone.
_ANALYST_WORD = re.compile(r"\b(?:analysts?|brokerages?|brokers?|strategists?|price targets?|ratings?|rated|coverage|"
                           r"upgrad\w*|downgrad\w*|initiat\w*|reiterat\w*|outperform\w*|underperform\w*|overweight|"
                           r"underweight|top pick|street[- ](?:high|low))\b", re.I)
# Nasdaq feeds: "<Issuer> - <Class> Price Target ..." for preferreds, debt, warrants, ADRs, bonds and units is not
# a common-stock analyst action; the phrase is masked so neither categories nor sentiment come from it.
_NONCOMMON_PT = re.compile(r"\s[-–—]\s+(?:preferred(?: stock| shares?)?|debt|equity warrants?|warrants?|"
                           r"depositary (?:receipts?|shares?)|corporate bonds?|bonds?|units?|notes?|rights?) price targets?\b",
                           re.I)
# "beat, but the stock falls": in a title the clause after a contrast connector states the outcome.
_CONTRAST = re.compile(r",\s*but\b|\bbut\b|(?<!not )\byet\b|\bhowever\b")
_FUND_TXT = re.compile(r"\b(?:ark|etfs?|funds?|positions?|holdings?|portfolio|rebalanc\w*|13f)\b", re.I)

# (regex, factor): factor scales the category weight (1.3 strong, 1 normal, <1 weak). Case-insensitive unless (?-i:).
_RULES: dict[str, list[tuple[str, float]]] = {
    "earnings": [
        (r"\b(?:q[1-4]|first|second|third|fourth)[- ]quarter (?:\d{4} |fiscal \d{4} )?(?:financial |operating )?(?:results|earnings|profit|revenue|sales|loss|eps)\b", 1.0),
        (r"\bq[1-4] (?:\d{4} |fy ?\d{2,4} )?(?:results|earnings|eps|revenue|sales|profit|loss|beat|miss)\b", 1.0),
        (r"\b(?:beats?|beat|tops?|topped|miss(?:es|ed)?|exceeds?|exceeded|surpass(?:es|ed)?|trails?|lags?) (?:\w+ ){0,3}" + _EST, 1.1, "macro"),
        (r"\b(?:beats?|tops?|misses?|missed) on (?:the )?(?:top|bottom)(?: and (?:top|bottom))? lines?\b|\b(?:profit|revenue|sales|eps|earnings|quarterly) (?:beat|miss)\b", 1.1),
        (r"\b(?:edged |came in )?(?:above|below) (?:\w+ ){0,2}(?:estimates|expectations|consensus)\b", 1.1, "macro"),
        (r"\b(?:fiscal|full[- ]year|annual|quarterly) (?:\d{4} )?(?:results|earnings|profit|loss)\b", 1.0),
        (r"\bearnings (?:beat|miss|report|results|call|preview|recap|surprise|release|date)\b", 1.0),
        (r"\b(?:reports?|reported|posts?|posted|announces?|delivers?|swings? to) (?:\w+ ){0,4}(?:financial results|quarter(?:ly)? results|earnings|profit|net (?:income|loss)|quarterly revenue)\b", 0.9),
        (r"\b(?:eps|earnings per share)\b", 0.8),
        (r"(?<!weekly )(?<!hourly )(?<!average )\bearnings\b", 0.7, "macro"),
        (r"\b(?:net income|net loss|adjusted ebitda|same-store sales|comparable sales)\b", 0.6),
        (r"(?-i:\bItem 2\.02\b)", 1.0),
    ],
    "guidance_up": [
        (r"\b" + _UPV + r" " + _GQ + _GW, 1.2),
        (r"\bbeat[- ](?:and|&)[- ]raise\b", 1.3),
        (r"\b(?:better|upbeat|strong|stronger|bullish|rosy|optimistic|above[- ]consensus|solid|robust|blowout) (?:\w+ ){0,2}" + _GW, 1.0),
        (r"\b(?:guidance|outlook|forecast) (?:raise|hike|tops|beats|above)\b", 1.1),
        (r"\b(?:sees|expects|forecasts?) (?:\w+ ){0,5}above (?:" + _EST + r"|wall street)\b", 1.0),
    ],
    "guidance_down": [
        (r"\b" + _DNV + r" " + _GQ + _GW, 1.2),
        (r"\b(?:weak|weaker|disappointing|soft|softer|downbeat|bleak|gloomy|cautious|below[- ]consensus|dismal|tepid|lackluster|grim) (?:\w+ ){0,2}" + _GW, 1.0),
        (r"\b(?:guidance|outlook|forecast) (?:cut|below|misses|disappoints|falls short)\b", 1.1),
        (r"\bprofit warning\b", 1.3),
        (r"\bwarns? (?:\w+ ){0,4}(?:profit|revenue|sales|earnings|demand|slowdown|margins?)\b", 1.0),
        (r"\bwarns? (?:of |on )?(?:\w+[- ]){0,3}(?:shortfall|loss|miss|decline|weakness)\b|\b(?:outlook|guidance|forecast) (?:fails? to impress|underwhelms?|disappoints?)\b", 1.1),
        (r"\b(?:sees|expects|forecasts?) (?:\w+ ){0,5}below (?:" + _EST + r"|wall street)\b", 1.0),
    ],
    "analyst_up": [
        (r"\bupgrade[sd]? (?:\w+ ){0,4}to (?:buy|outperform|overweight|strong buy|positive|accumulate|add)\b", 1.2),
        (_ANALYST_UPGRADE, 0.9),
        (r"\b" + _UPV + r" (?:\w+ ){0,4}price (?:target|objective)\b", 1.0),
        (r"\bprice target (?:raised|lifted|boosted|hiked|increased|to \$[\d.,]+ from \$[\d.,]+ \(up\))\b", 1.0),
        (r"\binitiat\w* (?:coverage )?(?:\w+ ){0,4}(?:at|with)(?: an?)? (?:buy|outperform|overweight|strong buy|positive|accumulate)\b", 1.0),
        (r"\b(?:reiterat|maintain)\w* (?:\w+ ){0,3}(?:buy|outperform|overweight)\b", 0.6),
        (r"\b(?:top pick|street[- ]high)\b", 0.7),
    ],
    "analyst_down": [
        (r"\bdowngrade[sd]? (?:\w+ ){0,4}to (?:sell|underperform|underweight|neutral|hold|equal[- ]weight|market perform|sector perform|reduce)\b", 1.2),
        (_ANALYST_DOWNGRADE, 0.9),
        (r"\b" + _DNV + r" (?:\w+ ){0,4}price (?:target|objective)\b", 1.0),
        (r"\bprice target (?:cut|lowered|slashed|trimmed|reduced|decreased|decreases|decreasing)\b", 1.0),
        (r"\binitiat\w* (?:coverage )?(?:\w+ ){0,4}(?:at|with)(?: an?)? (?:sell|underperform|underweight|reduce)\b", 1.0),
        (r"\bstreet[- ]low\b", 0.7),
    ],
    "mna": [
        (r"\bto acquire\b|\bto be acquired\b|\bacquired by\b|\bagree[sd]? to (?:buy|acquire|merge|combine)\b", 1.25),
        (r"\b(?:deal|plans?|in talks|offers?|bids?|moves?|seeks?|set|nears? (?:a )?deal|agreement) to (?:acquire|merge with|take over|combine with)\b|\b(?:deal|plans?|in talks|offers?|bids?|moves?|seeks?|set|nears? (?:a )?deal|agreement) to (?:buy|purchase) (?:(?!U\.S\.)(?-i:[A-Z])|(?:\w+ ){0,2}(?:stake|startup|rival|company|firm|maker|business|unit|division|assets|operator|chain|brand|developer|provider|producer|lender|bank|insurer)\b)", 1.2),
        (r"\bto buy (?-i:[A-Z])\S*(?: \S+){0,4} for (?:about |nearly |roughly |up to |an? )?\$", 1.25),
        (r"\b(?:merger|buyout|takeover|tender offer|take[- ]private|going private|hostile bid|all[- ]cash deal|all[- ]stock deal|per share in cash|merger agreement)\b", 1.0),
        (r"\b(?:acquires?|acquisition|merge|merges|merging|bid for)\b", 0.8),
        # "buys" only with a company object; fund / ETF holdings ("ARK buys ... shares") are not deals.
        (r"\bbuys (?:\S+ ){0,2}?(?:company|startup|firm|stake|rival|maker|business|unit)\b", 0.8, "fund"),
        # SPAC shells announce "business combinations" constantly: a weaker M&A signal than a takeover.
        (r"\bbusiness combination\b", 0.5),
        (r"\b(?:divest\w*|spin[- ]?off|spins off|carve[- ]out)\b|\bsells? (?:its )?(?:\w+ ){0,3}(?:unit|business|division)\b", 0.7),
        (r"(?-i:\bItem 2\.01\b)|\bm&a\b", 0.7),
    ],
    "fda": [
        (r"\bfda (?:approv\w*|clear\w*|grants?|granted|authoriz\w*|accepts?|accepted|rejects?|rejected|declines?|declined|nod)\b", 1.3),
        (r"\b(?:approved|cleared|authorized|rejected) by (?:the )?(?:u\.s\. )?(?:fda|food and drug administration)\b", 1.3),
        (r"\b(?:receives?|received|wins?|won|gets?|secures?|secured|granted|grants) (?:u\.s\. )?fda\b", 1.3),
        (r"\bfda'?s? (?:approval|clearance|authorization|decision|nod|green light)\b", 1.25),
        (r"\b(?:pdufa|complete response letter|breakthrough therapy|fast track designation|orphan drug designation|priority review|accelerated approval|emergency use authorization|510\(k\)|de novo)\b", 1.1),
        (r"\b(?:fda advisory (?:committee|panel)|adcom|advisory committee (?:votes?|backs|recommends))\b", 1.1),
        (r"\b(?:s?nda|bla|maa) (?:approval|submission|filing|accepted|acceptance)\b", 1.0),
        (r"\bfda\b|\bfood and drug administration\b", 0.6),
    ],
    "clinical": [
        (r"\b(?:met|meets|achieved|achieves|missed|misses|failed to meet|did not meet) (?:\w+ ){0,3}(?:primary|secondary) endpoints?\b", 1.3),
        (r"\b(?:positive|negative|mixed|encouraging|disappointing) (?:\w+ ){0,3}(?:topline|top-line|interim|phase|trial|study|clinical) (?:\w+ ){0,2}(?:results|data|readout)\b", 1.2),
        (r"\b(?:topline|top-line) (?:results|data)\b|\bdata readout\b|\bclinical hold\b", 1.1),
        (r"\bphase (?:1|2|3|i{1,3}|1/2|2/3|i/ii|ii/iii|1b|2a|2b|3b)\b", 0.9),
        (r"\b(?:trial|study) (?:results|data|met|meets|failed|fails|succeeds|halted|stopped)\b|\bprimary endpoint\b|\bpivotal (?:trial|study)\b", 1.0),
        (r"\bclinical trial\b|\binterim (?:analysis|data|results)\b|\bfirst patient dosed\b|\bind clearance\b", 0.7),
    ],
    "offering": [
        (r"\b(?:prices?|priced|pricing) (?:\w+ ){0,5}offering\b|\bregistered direct\b|\bat[- ]the[- ]market (?:offering|program|equity)\b", 1.25),
        (r"(?<!initial )\b(?:public|secondary|follow-on|underwritten|best[- ]efforts|common stock|equity|share|stock|proposed|upsized|overnight) offering\b", 1.15),
        (r"\b(?:private placement|shelf registration|atm (?:program|offering|facility)|equity raise|capital raise)\b", 1.0),
        (r"\b(?:convertible (?:senior )?notes? offering|offering of (?:\$[\d.,]+ (?:million|billion) )?(?:\w+ ){0,3}(?:shares|common stock|convertible))\b", 1.0),
        (r"\b(?:raises?|raised|raising) \$[\d.,]+ ?(?:million|mln|m|billion|bn)\b", 0.7),
        (r"\b(?:dilution|dilutive|warrant (?:exercise|inducement)|convertible notes?)\b", 0.7),
        (r"(?-i:\b(?:S-3|424B\d|F-3)\b)", 0.6),
    ],
    "buyback": [
        (r"\b(?:share|stock) (?:buyback|repurchase)s?\b|\b(?:buyback|repurchase) (?:program|plan|authorization)\b", 1.2),
        (r"\b(?:buybacks?|repurchases?|buy back)\b", 1.0),
    ],
    "dividend": [
        (r"\b(?:raises?|raised|increases?|increased|hikes?|hiked|boosts?|cuts?|slashes?|suspends?|eliminates?|initiates?|declares?|declared) (?:\w+ ){0,3}dividends?\b", 1.2),
        (r"\bspecial dividend\b|\bdividend (?:hike|increase|cut|suspension)\b", 1.2),
        (r"\bdividends?\b|\b(?:quarterly|monthly) distribution\b|\bex-dividend\b", 0.8),
    ],
    "insider": [
        (r"\binsiders? (?:buy\w*|sell\w*|sales|purchas\w*|trading|transactions?|bought|sold|dump\w*)\b|\binsider (?:buying|selling|sales)\b|\b(?:co-?founders?|ceo|cfo|insiders?|directors?|executives?) (?:\w+ ){0,2}load(?:s|ed)? up on\b", 1.2),
        (r"\b(?:ceo|cfo|coo|chairman|chairwoman|director|founder|president|chief \w+ officer|insider|executive|exec|billionaire) (?:\w+ ){0,3}(?:buys|bought|sells|sold|purchases|purchased|dumps|unloads|offloads|trims) (?:\$?[\d.,]+ ?(?:million|m|k)? )?(?:\w+ ){0,3}(?:shares|stock|stake)\b", 1.2),
        (r"(?-i:\bForm 4\b)", 1.0),
    ],
    "contract": [
        (r"\b(?:wins?|won|awarded|secures?|secured|lands?|landed|receives?|received|signs?|signed|inks?|inked) (?:\w+ ){0,4}(?:contract|order|award)s?\b", 1.2),
        (r"\b(?:pentagon|department of (?:defense|war)|army|navy|air force|nasa|darpa|government) (?:\w+ ){0,2}(?:contract|award|deal|order)\b", 1.2),
        (r"\bcontract (?:award|win|worth|valued)\b|\b(?:multi-?year|strategic|exclusive|supply|licensing|distribution|offtake) (?:agreement|deal|contract)\b", 1.0),
        (r"\b(?:signs?|signed|inks?|inked|enters? into|entered into) (?:\w+ ){0,3}(?:agreement|deal|partnership)\b|\bdefinitive agreement\b", 0.9),
        (r"\bpartners? with\b|\bpartnership\b|\bcollaborat\w+\b|\bjoint venture\b", 0.7),
        (r"(?-i:\bItem 1\.01\b)", 0.8),
    ],
    "lawsuit": [
        (r"\b(?:sec|doj|ftc|justice department|prosecutors|regulators|attorney general|fbi|cftc|finra) (?:\w+ ){0,2}(?:charges?|charged|sues?|sued|probe|probes|probing|investigat\w+|subpoena\w*|raid\w*)\b", 1.25),
        (r"\b(?:lawsuit|sues?|sued|suing|class[- ]action|antitrust|indict\w*|subpoena\w*|wells notice|consent decree|whistleblower|securities fraud)\b", 1.0),
        (r"\b(?:probe|investigat\w*|litigation|settlement|settles?|settled|verdict|jury|fined|fines? of \$|penalty|injunction)\b", 0.8),
        (r"\b(?:court|judge|ruling|appeal|appeals court|supreme court)\b", 0.5),
    ],
    "accounting": [
        (r"\bauditor (?:\w+ ){0,2}(?:resign\w*|quits?|departure|exits?|dismiss\w*)\b|\b(?:accounting (?:irregularities|probe|issues|errors|scandal|review)|material weakness|restatement|restate\w*)\b", 1.2),
        (r"\b(?:delays?|delayed|late) (?:\w+ ){0,2}(?:10-k|10-q|annual report|quarterly report|filing)\b|\bnon-reliance\b|(?-i:\bItem 4\.02\b)", 1.0),
    ],
    "bankruptcy": [
        (r"\b(?:files?|filed|filing|seeks?|preparing|prepares|nears?) (?:\w+ ){0,2}(?:for )?(?:bankruptcy|chapter (?:11|7|15))\b", 1.3),
        (r"\b(?:bankrupt\w*|chapter (?:11|7|15)|insolven\w*|going concern|receivership|creditor protection)\b", 1.1),
        (r"\b(?:liquidat(?:e|es|ed|ing)|liquidation) (?:of )?(?:its |the |all )?(?:assets|business|company|operations|subsidiar\w*)\b|"
         r"\b(?:plan of|process of|under) liquidation\b|\bliquidation (?:plan|process|sale|value|basis)\b|"
         r"\bwind(?:ing)? down (?:its |the )?(?:operations|business|company)\b|"
         r"\b(?:default(?:s|ed)? on|debt restructuring|missed (?:\w+ ){0,2}(?:interest|debt) payment)\b", 0.8),
        (r"\brestructuring advis[eo]rs?\b|\bcreditor (?:talks|group|committee)s?\b", 0.8),
        (r"(?-i:\bItem 1\.03\b)", 1.0),
    ],
    "layoffs": [
        (r"\blay(?:s|ing)? off\b|\blayoffs?\b|\blaid off\b|\bjob cuts?\b|\bworkforce reduction\b|\breduce (?:its )?workforce\b", 1.1),
        (r"\b(?:eliminat|slash|cut|reduc|trim)\w* (?:about |nearly |roughly |some |over |more than |up to )?(?:\d[\d,]* |\d+(?:\.\d+)?% of (?:its |the |their )?(?:global |total |worldwide |corporate |\w+ )?)?(?:jobs|positions|roles|employees|workers|staff|workforce|headcount)\b", 1.1),
        (r"\bheadcount\b|\brestructuring plan\b|\brestructuring (?:that|to) cut\b", 0.6),
    ],
    "macro": [
        (r"\b(?:cpi|ppi|pce|inflation|payrolls|jobs report|gdp|retail sales|jobless claims|unemployment rate|consumer prices|producer prices|wholesale prices|import prices|core prices|(?:consumer |producer |import )?price index|(?:flash )?pmi|(?:business|factory|manufacturing|services) activity) (?:\w+ ){0,3}(?:rises?|rose|jumps?|jumped|falls?|fell|cools?|cooled|heats?|accelerat\w+|slows?|slowed|eases?|eased|unexpectedly|surges?|surged|beats?|miss\w*|tops?|hotter|cooler|stronger|weaker|grew|shrank|contracted|contracts?|expand\w*|ticks?|edges?|up|down|increase[sd]?|decrease[sd]?|holds?|held)\b", 1.3),
        (r"\b(?:hotter|cooler|stronger|weaker|softer|better|worse)[- ]than[- ]expected (?:inflation|cpi|jobs|payrolls|gdp|retail sales|data|report)\b", 1.3),
        (r"\b(?:cpi|ppi|pce|jolts|nonfarm|non-farm|payrolls|payroll (?:report|data|growth)|jobless claims|unemployment (?:rate|claims)|jobs report|retail sales|consumer price index|producer price|price index|consumer (?:confidence|sentiment|spending)|inflation|gdp|pmi|flash pmi|ism (?:manufacturing|services)|(?:business|factory|manufacturing|services) activity|housing starts|home sales|durable goods|industrial production|trade deficit|core prices|wage growth|labor market|beige book|economic data|consumer prices|producer prices|wholesale prices|wholesale inflation)\b", 1.0),
        (r"\b(?:treasury yields?|bond yields?|(?:2|5|10|30)-year (?:treasury|yield|note)s?|yield curve|dollar index|stagflation|recession|government shutdown|debt ceiling|deficit)\b", 0.75),
    ],
    "fed": [
        (r"\b(?:fed|federal reserve|fomc|powell) (?:(?!to |could |may |might |will |would |should |seen |expected |likely |set )\w+ ){0,2}(?:cuts?|raises?|hikes?|holds?|keeps?|leaves?|lowers?|slashes?|pauses?|delivers?) (?:\w+ ){0,3}(?:rates?|interest rates|steady|unchanged|basis points?)\b", 1.5),
        (r"\b(?:fed|fomc) (?:rate )?decision\b|\bfomc (?:statement|minutes)\b|\bdot plot\b|\bpowell (?:says|said|signals|warns|testimony|speech|press conference)\b", 1.2),
        (r"\brate (?:cut|hike|decision)s?\b|\b(?:cuts?|raises?|hikes?) (?:interest )?rates\b", 0.9),
        (r"\b(?:fomc|federal reserve|fed chair\w*|monetary policy|quantitative (?:tightening|easing)|jackson hole)\b|(?-i:\bPowell\b)", 1.0),
        # "Fed" needs policy context nearby ("Fed Cattle Exchange" is not the central bank).
        (r"(?-i:\bFed\b)(?! [Cc]attle)(?=(?:\W+\w+){0,5}?\W+" + _FEDCTX + r"\b)|\b" + _FEDCTX + r"\b(?:\W+\w+){0,5}?\W+(?-i:Fed)\b(?! [Cc]attle)|(?-i:\b(?:the Fed|Fed's)\b)(?! [Cc]attle)", 1.0),
        (r"(?-i:\b(?:Waller|Bowman|Goolsbee|Bostic|Kashkari|Musalem|Hammack|Barkin|Warsh|Hassett)\b)", 0.7),
        (r"\b(?:ecb|boj|bank of japan|bank of england|central banks?|basis points?|interest rates?)\b", 0.6),
    ],
    "ipo": [
        # An IPO event (files / prices / plans / scraps / debuts), not any mention ("Since Its IPO Year", "OpenAI IPO").
        (r"\b(?:files?|filed|filing|prices?|priced|pricing|launch\w*|plans?|planning|seeks?|pursu\w*|pull\w*|scrap\w*|delay\w*|postpone\w*|pause\w*|shelve\w*|sets? terms (?:for|of))\b(?:\s+[\w.&'\-]+){0,4}?\s+(?:ipo|initial public offering)\b|\bipo (?:pric\w+|filing|plans?|debut|launch|terms)\b|\b(?:market|nasdaq|nyse|trading|stock market) debut\b|\bdirect listing\b|\bgoes public\b|\bto go public\b", 1.2),
        (r"\b(?:files?|filed|filing) (?:confidentially )?(?:for|to) (?:an? )?(?:ipo|go public|list)\b", 1.0),
        (r"(?<!post-)\b(?:ipo|initial public offering)\b(?! (?:year|price|leaders?|index|etf|stocks?|class|market))|\bblank[- ]check\b|(?-i:\bSPAC\b)", 0.4, "ipo_hist"),
        (r"\b(?:shares?|stock|ipo|listing) debut\w*\b|(?-i:\b(?:S-1|F-1)\b)", 0.6),
    ],
    "halt": [
        (r"\btrading (?:halt|halted|suspended|pause|paused)\b|\bhalt(?:s|ed)? trading\b|\b(?:shares|stock|trading) (?:were |was |is |are )?(?:halted|suspended)\b", 1.2),
        (r"\b(?:circuit breaker|volatility (?:halt|pause)|limit up-limit down|news pending|t1 halt)\b|(?-i:\bLULD\b)", 1.1),
    ],
    "short_report": [
        (r"\bshort[- ]sellers? (?:\w+ ){0,3}(?:report|says|accuses|alleges|targets|bets)\b|\bshort (?:report|attack|thesis|campaign)\b", 1.2),
        (r"\bshort[- ]sellers?\b", 0.4),
        (r"\b(?:hindenburg|muddy waters|citron|spruce point|grizzly research|fuzzy panda|iceberg research|culper|wolfpack|kerrisdale|scorpion capital|blue orca|viceroy research|bonitas|j capital|gotham city|morpheus research)\b", 1.2),
        (r"\b(?:short interest|bets? against|short position)\b", 0.5),
    ],
    "split": [
        (r"(?<!reverse )\b(?:\d+[- ]for[- ]1 )?(?:stock|share) split\b|\bforward split\b|\b\d+-for-1 split\b", 1.1),
    ],
    "reverse_split": [
        (r"\breverse (?:stock |share )?split\b|\b1[- ]for[- ]\d+ (?:reverse )?split\b", 1.1),
    ],
    "delisting": [
        (r"\bdelist\w*\b|\b(?:minimum bid|bid price) (?:requirement|rule|deficiency|notice)\b", 1.1),
        (r"\b(?:nasdaq|nyse) (?:\w+ ){0,3}(?:deficiency|non-?compliance|noncompliance)\b|\bregain\w* compliance\b|\bnotice of (?:delisting|non-?compliance)\b", 1.0),
        (r"(?-i:\bItem 3\.01\b)", 1.0),
    ],
    "filing": [
        (r"(?-i:\b(?:8-K|8-K/A|10-Q|10-K|10-K/A|6-K|20-F|40-F|S-1|S-3|S-4|F-1|F-3|424B\d|DEF 14A|SC 13[DG]|13F|Form [348])\b)", 1.0),
        (r"\b(?:sec filing|regulatory filing|files with the sec|filed with the sec)\b", 0.8),
    ],
    "product": [
        (r"\b(?:launch\w*(?! (?:a |an |its |the )?(?:formal |strategic |full |internal )?(?:review|probe|investigation|inquiry|tender|offer|bid|search|lawsuit|campaign|ipo))|unveil\w*|introduc\w*|rolls? out|rolled out|releases? (?:new|its)|announces? new|new (?:product|model|chip|phone|device|platform|service|feature|drug|vehicle))\b", 0.9),
        (r"\b(?:iphone|ipad|vision pro|blackwell|rubin|robotaxi|cybertruck|optimus|full self-driving|copilot|chatgpt|ai model|ai agents?)\b", 0.6),
    ],
    "crypto": [
        (r"\b(?:bitcoin|ethereum|crypto\w*|stablecoins?|blockchain|solana|dogecoin|binance|defi|altcoins?|memecoins?)\b|(?-i:(?<![\w-])(?:BTC|XRP|ETH)(?![\w-]))", 1.0),
        (r"\bether\b", 0.6),
    ],
    "geopolitics": [
        (r"\b(?:imposes?|imposed|announces?|slaps?|raises?|hikes?|threatens?|retaliat\w*|escalat\w*|new|sweeping|reciprocal|\d+%) (?:\w+ ){0,2}tariffs?\b|\btariffs? on (?:all |\w+ ){0,2}(?:imports|goods|china|chinese|canada|mexico|eu|europe|japan)\b|\btrade (?:war|truce|deal (?:reached|signed|collapses))\b|\b(?:new )?export (?:controls?|curbs?|ban)\b|\bchip (?:ban|curbs|restrictions)\b|\bentity list\b", 1.2),
        (r"\b(?:tariffs?|trade (?:deal|talks|tensions|agreement|dispute|negotiations|pact)|export restrictions?)\b", 0.8),
        (r"\b(?:sanctions?|geopolitic\w*|invasion|missile|airstrikes?|ceasefire|military strike|troops)\b", 1.0),
        (r"\b(?:war|china|beijing|xi jinping|taiwan|russia|ukraine|iran|israel|gaza|north korea|middle east|nato)\b", 0.55),
        (r"\b(?:white house|trump|congress|senate)\b", 0.4),
    ],
    "management": [
        (r"\b(?:ceo|cfo|coo|cto|chief executive|chief financial officer|chairman|chairwoman|president|chair) (?:\w+ ){0,3}(?:resigns?|resigned|steps? down|stepping down|departs?|departure|retires?|retiring|retirement|ousted|fired|exits?|to leave|leaves|replaced|succession)\b", 1.2),
        (r"\b(?:ousts?|ousted|fires?|fired|replaces?|dismiss(?:es|ed)?|parts? ways with|parted ways with) (?:its |the )?(?:ceo|cfo|coo|chief executive|chief financial officer|chairman|president)\b", 1.2),
        (r"\b(?:names?|named|appoints?|appointed|hires?|hired|taps?|tapped) (?:\w+ ){0,4}(?:as )?(?:new )?(?:ceo|cfo|coo|chief executive|chief financial officer|chairman|chairwoman|president)\b|\bnew ceo\b", 1.0),
        (r"\b(?:management shake-?up|board shake-?up|leadership change|succession plan)\b|(?-i:\bItem 5\.02\b)", 0.9),
    ],
    "activist": [
        (r"\bactivist(?: investor| hedge fund| stake| shareholder| campaign)?\b|\bproxy (?:fight|battle|contest)\b", 1.1),
        (r"\b(?:takes?|took|builds?|built|discloses?|disclosed|boosts?|boosted|raises?|raised) (?:an? )?(?:\$?[\d.,]+ ?(?:million|billion|%)? )?(?:\w+ ){0,2}stake in\b", 1.0),
        (r"\b" + _ACTIVISTS + r"\b(?:\W+\w+){0,6}?\W+" + _ACTIVIST_ACT + r"|\b" + _ACTIVIST_ACT + r"(?:\W+\w+){0,6}?\W+" + _ACTIVISTS + r"\b|(?-i:\bSC 13D\b|\b13D\b)", 1.0),
        (r"\b" + _ACTIVISTS + r"\b", 0.4),
        (r"\bboard seats?\b", 0.7),
    ],
    "commodities": [
        (r"\b(?:oil|crude|gold|silver|copper|natural gas|gasoline) (?:prices?|futures|rall\w+|jumps?|surges?|slumps?|falls?|plunges?|climbs?|hits?|tops?)\b|\bopec\+?\b", 1.2),
        (r"\b(?:oil|crude|brent|wti|natural gas|gold|silver|copper|platinum|palladium|lithium|uranium|wheat|corn|soybeans?|cocoa|coffee|iron ore)\b", 0.7),
    ],
    "index_change": [
        (r"\b(?:to join|joins|added to|to be added to|will be added to|set to join|replac\w+ \w+(?: \w+){0,3} in) (?:the )?(?:s&p (?:500|midcap 400|smallcap 600)|nasdaq[- ]100|dow jones industrial average|russell (?:1000|2000|3000))\b", 1.2),
        (r"\b(?:index (?:inclusion|addition|rebalanc\w*)|removed from (?:the )?(?:s&p 500|nasdaq-100))\b", 1.0),
    ],
    "market": [
        (r"(?:^|[:;] )(?:us |u\.s\. |global |world |asian |european )?(?:stocks|futures|equities)\b|\bsector update\b", 0.8),
        (r"\b(?:stock market|wall street|s&p 500|nasdaq composite|dow jones|the dow|dow|nasdaq|russell 2000|us stocks|u\.s\. stocks|global stocks|world stocks|equities|tech stocks|bank stocks|chip stocks|energy stocks) (?:today|rall\w+|falls?|fell|slides?|slid|closes?|closed|ends?|ended|opens?|opened|higher|lower|records?|climbs?|drops?|tumbles?|jumps?|sinks?|surges?|plunges?|slips?|rise|rises|rose|gains?|edges?|futures)\b", 1.1),
        (r"\b(?:stock futures|index futures|futures (?:rise|fall|slip|climb|edge|point|jump|sink|tumble|rally))\b|\bmarket (?:wrap|close|open|recap|rally|selloff|sell-off|rout)\b", 1.0),
        (r"\b(?:s&p 500|nasdaq composite|nasdaq 100|dow jones|russell 2000|magnificent seven|mag 7|big tech)\b|(?-i:\bVIX\b)", 0.5),
    ],
    "price_move": [
        (r"\b(?:shares?|stocks?|stock price)\b (?:\w+ ){0,2}(?:soar|surg|jump|rall|climb|spik|rocket|skyrocket|pop|plung|tumbl|sink|sank|slump|crater|plummet|div|tank|nosediv|crash|zoom|explod|halv)\w*", 1.1),
        (r"\b(?:soars?|soared|surges?|surged|jumps?|jumped|plunges?|plunged|tumbles?|tumbled|sinks?|sank|craters?|cratered|plummets?|plummeted|skyrockets?|skyrocketed|rockets?|rocketed|tanks?|tanked|spikes?|spiked|nosedives?|crashes|crashed)\b", 0.9, "nonequity"),
        (r"\b(?:52-week|all-time|record|multi-?year|\d+-(?:year|month)|(?:one|two|three|five|six|ten)-(?:year|month)) (?:high|low)s?\b|\bstock (?:is )?(?:up|down|soaring|sinking|surging|plunging|jumping|falling|rising|crashing) today\b|\b(?:biggest|best|worst) (?:day|week|month|drop|gain|decline|rally|selloff) (?:since|in|ever)\b", 1.0, "nonequity"),
        (r"\b\d{2,3}(?:\.\d+)?% (?:higher|lower|gain|drop|jump|surge|plunge|rise|fall|decline|rally|slump)\b|\b(?:up|down|higher|lower|gains?|falls?|fell|rises?|rose|drops?|dropped|jumps?|jumped|surges?|surged|plunges?|plunged|sinks?|sank|soars?|soared|slides?|slid|slips?|slipped|climbs?|climbed|rall(?:y|ies|ied)|tumbles?|tumbled|crash(?:es|ed)?) (?:as much as |more than |nearly |over |about )?(?:[2-9]|[1-9]\d{1,2})(?:\.\d+)?%", 1.0, "nonequity"),
        (r"\b(?:premarket|pre-market|pre-bell|after-hours|afterhours|extended trading|unusual (?:options )?(?:activity|volume)|biggest movers|stocks making the biggest moves|top movers)\b", 0.6),
    ],
    "ai": [
        (r"\b(?:artificial intelligence|generative ai|genai|ai chips?|ai (?:model|agents?|data cent(?:er|re)s?|infrastructure|spending|capex|bubble|boom|trade)|data cent(?:er|re)s?|large language models?|openai|anthropic|chatgpt|agentic)\b|(?-i:\bAI\b(?!\s+(?:HOLDINGS|INC|CORP|LTD|GROUP|SYSTEMS)\b))", 1.0),
    ],
}

# Listicles / opinion / promotional pieces: strongly dampened importance.
_OPINION = re.compile("|".join([
    r"^\s*(?:\d+|three|four|five|six|seven|eight|nine|ten|two)\s+(?:[\w&'.\-]+\s+){0,4}(?:stocks?|shares|etfs?|reasons|things|ways|picks|dividend|growth|ai|tech|companies|lessons|signs|mistakes)\b",
    r"\b(?:stocks?|shares|etfs?) to (?:buy|watch|sell|own|avoid|hold|consider)\b",
    r"\b(?:is|are) (?:it|now|this|these|they)?\s*(?:\w+\s+){0,4}(?:a |an )?(?:good |smart |great |strong |screaming |buy |bargain )?(?:buy|sell|bargain|steal|stock to buy)\b.*\?\s*$",
    r"^\s*(?:should|could|can|will|would|is|are|does|do|has|have|why|what|how|where|which|who)\b[^?]{3,140}\?\s*$",
    r"\bshould you (?:buy|sell|invest|be worried|worry)\b|\bbetter (?:buy|stock|investment)\b|\bwhich is (?:the )?better\b",
    r"\b(?:(?:for|in|during|into|before) retirement|retirement (?:portfolio|income|account|savings|stocks?|plan)|retire (?:early|rich|a millionaire|on)|millionaire|passive income|forever|no-brainer|buy and hold|dirt cheap|bargain|my favorite|i'?m buying|i bought|i'?m selling|surefire|life-changing|generational|hand over fist|set for life|could make you|monster stock|magnificent seven stock to buy)\b",
    r"\bprediction:?\b|\bhere'?s (?:why|what|how|the|my|our|where|when)\b|\bif you(?:'d| had)? invested\b|\byou(?:'ll)? (?:wish|regret)\b",
    r"\b(?:top|best) (?:\w+ ){0,2}(?:stocks?|picks|etfs?) (?:for|to|in|of|under)\b|\bstock(?:s)? under \$\d+\b",
    r"\b(?:down|up) \d+%(?:,| and| in| this| over)? (?:\w+ ){0,4}(?:is it|should|time to|buy)\b",
    r"\b(?:sponsored|advertorial|paid promotion|investorplace|motley fool)\b",
    r"\b(?:undervalued|overvalued|fair value|gf value|intrinsic value|valuation check|trade at a discount|looks? cheap|could be cheap|still a bargain|already pricey|priced in|looks reasonable|bull case|bear case|investment case|investment thesis|change the case for|hopeless|ready for a rebound|time to buy|too late to buy|still a buy|worth buying)\b",
    r"\bstock (?:may|could|might) (?:be|trade|look)\b|\bstock price prediction\b|\bprice prediction\b",
]), re.I)
_ANALYSIS = re.compile(r"^\s*(?:analysis|opinion|commentary|column|explainer|editorial|perspective|viewpoint|op-ed|"
                       r"earnings call transcript|transcript|podcast|video|watch|live updates?)\s*[:|\-]|\?\s*$", re.I)
_FOREIGN = re.compile(
    r"\b(?:uk|u\.k\.|britain|british|india|indian|japan|japanese|euro ?zone|europe|european|germany|german|france|"
    r"french|canada|canadian|australia|australian|brazil|mexico|korea|korean|philippines?|philippine|turkey|turkish|"
    r"indonesia|indonesian|china|chinese|hong kong|singapore|swiss|switzerland|sweden|swedish|italy|italian|spain|"
    r"ecb|boj|bank of england|bank of japan|rbi|london|ftse|nikkei|hang seng|sensex|nifty|dax|rupee|yen|pound|"
    r"sterling|euro|pc|crore|lakh)\b", re.I)
_US = re.compile(r"\b(?:u\.s\.|us|united states|america|american|fed|fomc|federal reserve|powell|treasury|treasuries|"
                 r"wall street|s&p|dow|nasdaq|nyse|russell|washington|white house|trump|congress|irs|sec)\b", re.I)
_OPINION_PUBLISHERS = re.compile(
    r"motley fool|investorplace|24/7 wall|zacks|marketbeat|gurufocus|simply wall|insider monkey|tipranks|"
    r"finbold|ainvest|stocktwits|benzinga pro|the street pro|validea|investing\.com academy|nasdaq\.com/articles|"
    r"seeking alpha|timothy sykes|stockstotrade|tradingkey|capmad|kucoin|biggo|kalkine|trefis|ap7am|"
    r"startup fortune|investorsobserver|coincentral|economies\.com|fxstreet|ts2\.tech|meyka|stock titan", re.I)
_LAWFIRM = re.compile(
    r"\b(?:law (?:firm|offices?)|shareholder (?:alert|rights)|investor (?:alert|notice)|deadline alert|"
    r"lead plaintiff deadline|class action (?:filed|lawsuit) (?:on behalf|against)|encourages? (?:\w+ ){0,3}investors|"
    r"investors who lost|reminds? (?:\w+ ){0,3}investors|rosen law|pomerantz|levi & korsinsky|bragar eagel|faruqi|"
    r"glancy|kessler topaz|bronstein|schall law|robbins geller|rigrodsky|kirby mcinerney|hagens berman|"
    r"bernstein liebhard|gainey mckenna|johnson fistel|holzer|frank r\. cruz|howard g\. smith|portnoy law|"
    r"block & leviton|bleichmar|halper sadeh|ademi|monteverde|brodsky & smith|kuehn law|lowey dannenberg|"
    r"gross law firm|wolf haldenstein|labaton|edelson lechtzin|bernstein litowitz|saxena white|"
    r"kahn swick|lewis kahn|application deadline|notice of (?:\w+ ){0,6}deadline|investigation notice|"
    r"class action (?:law)?suit\s*[-–—:|]\s*contact|contact\b[^.]{0,40}\besq\b)\b", re.I)
# Law-firm boilerplate opening a press release ("... notifies investors", "on behalf of all shareholders").
_LAWFIRM_OPEN = re.compile(r"\b(?:notifies|notifying|on behalf of (?:all )?)\s*(?:\w+\s+){0,3}(?:investors|shareholders|stockholders)\b",
                           re.I)

# ---------------------------------------------------------------- sentiment lexicon (Loughran–McDonald style)
_POS_WORDS = {
    "beat": 1.0, "outperform": 1.2, "outperformance": 1.0, "surge": 1.5, "soar": 1.8, "jump": 1.2, "rally": 1.2,
    "gain": 0.8, "rise": 0.6, "rose": 0.6, "risen": 0.6, "climb": 0.8, "rebound": 1.0, "recover": 0.8,
    "recovery": 0.8, "boost": 0.8, "upgrade": 1.4, "strong": 0.8, "stronger": 1.0, "strongest": 1.0, "robust": 1.0,
    "record": 0.5, "profitable": 1.0, "profitability": 0.6, "growth": 0.5, "grow": 0.5, "expand": 0.5,
    "expansion": 0.5, "accelerate": 0.6, "approval": 1.2, "approve": 1.2, "breakthrough": 1.0, "success": 1.0,
    "successful": 1.0, "successfully": 1.0, "win": 0.8, "won": 0.8, "award": 0.6, "positive": 0.8,
    "optimistic": 1.0, "optimism": 1.0, "bullish": 1.2, "upbeat": 1.2, "upside": 0.8, "overweight": 0.8,
    "improve": 0.6, "improvement": 0.6, "momentum": 0.5, "milestone": 0.6, "surpass": 1.0, "exceed": 1.0,
    "lift": 0.6, "advance": 0.6, "solid": 0.6, "healthy": 0.5, "resilient": 0.6, "resilience": 0.6,
    "favorable": 0.8, "benefit": 0.5, "gainer": 0.8, "winner": 0.8, "skyrocket": 2.0, "rocket": 1.5,
    "spike": 0.6, "pop": 0.8, "reaffirm": 0.4, "confident": 0.6, "confidence": 0.5, "excellent": 1.0,
    "exceptional": 1.0, "impressive": 1.0, "outstanding": 1.0, "stellar": 1.2, "blowout": 1.5,
    "blockbuster": 1.2, "booming": 1.0, "boom": 0.8, "thrive": 0.8, "upturn": 0.8, "uptrend": 0.8,
    "secure": 0.4, "tailwind": 0.8, "cheer": 0.8, "praise": 0.6, "best": 0.4, "better": 0.5, "beneficial": 0.6,
    "attractive": 0.5, "regain": 0.6, "higher": 0.5, "highs": 0.5, "accretive": 1.0, "outpace": 0.8,
    "partnership": 0.3, "rallies": 1.2, "zoom": 0.6, "upbeat": 1.2, "triumph": 1.0, "bolster": 0.7,
    "strength": 0.6, "strengthen": 0.6, "upgraded": 1.4, "buoy": 0.8, "buoyed": 0.8, "soaring": 1.8,
    "profit": 0.3, "wins": 0.8, "tops": 0.6, "topped": 0.6,
}
_NEG_WORDS = {
    "miss": 1.2, "fall": 0.8, "fell": 0.8, "fallen": 0.8, "drop": 0.9, "plunge": 2.0, "plummet": 2.0,
    "tumble": 1.6, "sink": 1.4, "sank": 1.4, "sunk": 1.4, "slump": 1.4, "slide": 1.0, "slid": 1.0,
    "decline": 0.8, "crash": 2.0, "tank": 1.5, "selloff": 1.2, "sell-off": 1.2, "loss": 0.8, "losses": 0.8,
    "lose": 0.8, "lost": 0.8, "weak": 1.0, "weaker": 1.0, "weakest": 1.0, "weakness": 1.0, "downgrade": 1.4,
    "cut": 0.6, "slash": 1.0, "warn": 1.0, "warning": 1.0, "risk": 0.4, "concern": 0.6, "worry": 0.7,
    "worried": 0.7, "fear": 0.7, "uncertainty": 0.6, "uncertain": 0.5, "volatile": 0.4, "volatility": 0.4,
    "lawsuit": 1.0, "sue": 1.0, "sued": 1.0, "litigation": 0.8, "investigation": 0.9, "investigate": 0.8,
    "probe": 0.9, "fraud": 1.8, "fraudulent": 1.8, "scandal": 1.5, "recall": 1.0, "delay": 0.8, "halt": 0.8,
    "suspend": 1.0, "suspension": 1.0, "bankrupt": 2.5, "bankruptcy": 2.5, "insolvency": 2.2, "insolvent": 2.2,
    "default": 1.5, "layoff": 0.6, "layoffs": 0.6, "restructuring": 0.4, "impairment": 1.0, "writedown": 1.0,
    "write-down": 1.0, "deficit": 0.5, "deficiency": 1.0, "delist": 1.5, "delisting": 1.5, "dilution": 1.2,
    "dilutive": 1.2, "downturn": 1.0, "recession": 1.0, "slowdown": 0.9, "slowing": 0.6, "stagnation": 0.8,
    "contraction": 0.8, "shrink": 0.8, "underperform": 1.2, "underweight": 0.8, "bearish": 1.2,
    "pessimistic": 1.0, "pessimism": 1.0, "negative": 0.8, "adverse": 1.0, "disappoint": 1.3,
    "disappointing": 1.3, "disappointment": 1.3, "fail": 1.2, "failure": 1.2, "reject": 1.2, "rejection": 1.2,
    "deny": 0.6, "terminate": 0.8, "termination": 0.8, "cancel": 0.6, "cancellation": 0.6, "breach": 1.0,
    "hack": 0.8, "cyberattack": 1.0, "outage": 0.8, "shortage": 0.6, "shortfall": 1.0, "headwind": 0.8,
    "pressure": 0.5, "struggle": 0.8, "troubled": 1.0, "trouble": 0.8, "crisis": 1.2, "turmoil": 1.2,
    "chaos": 1.0, "collapse": 2.0, "worst": 1.0, "worse": 0.8, "worsen": 0.9, "deteriorate": 1.0,
    "deterioration": 1.0, "erode": 0.7, "erosion": 0.7, "lag": 0.6, "laggard": 0.8, "loser": 0.8,
    "penalty": 0.8, "fined": 1.0, "sanction": 0.5, "tariff": 0.5, "war": 0.6, "conflict": 0.4, "attack": 0.5,
    "cautious": 0.5, "caution": 0.5, "overvalued": 0.8, "bubble": 0.6, "accuse": 1.0, "allegation": 1.0,
    "alleged": 0.8, "violation": 1.0, "misconduct": 1.2, "resign": 0.7, "oust": 1.0, "slip": 0.6, "dip": 0.4,
    "retreat": 0.6, "erase": 0.6, "wipe": 0.8, "bleed": 1.0, "crater": 2.0, "nosedive": 2.0, "freefall": 2.0,
    "rout": 1.5, "bloodbath": 2.0, "carnage": 1.5, "panic": 1.2, "meltdown": 1.8, "dive": 1.2, "skid": 0.8,
    "sour": 0.8, "sputter": 0.8, "stall": 0.7, "woes": 1.0, "woe": 1.0, "bleak": 1.0, "gloomy": 1.0,
    "grim": 1.0, "dismal": 1.2, "tepid": 0.8, "sluggish": 0.8, "downbeat": 1.2, "underwhelm": 1.2,
    "falter": 1.0, "hurt": 0.8, "damage": 0.8, "lower": 0.4, "losing": 0.8, "plunging": 2.0, "sinking": 1.4,
    "tumbling": 1.6, "slumping": 1.4, "sliding": 1.0, "falling": 0.8, "dropping": 0.9, "crashing": 2.0,
    "tanking": 1.5, "bearish": 1.2, "weigh": 0.6, "weighs": 0.6, "weighed": 0.6, "drag": 0.6, "dragged": 0.6,
}
_NEGATORS = {"not", "no", "never", "without", "neither", "nor", "none", "hardly", "barely", "isn't", "wasn't",
             "aren't", "doesn't", "didn't", "don't", "won't", "can't", "cannot", "unable", "fails", "failed",
             "fail", "lack", "lacks"}

# Weighted phrases (matched first, their span is masked from word scoring).
_PHRASES: list[tuple[str, float]] = [
    (r"\bbeat[- ](?:and|&)[- ]raise\b", 3.5),
    (r"\b(?:beats?|beat|tops?|topped|exceeds?|exceeded|surpass(?:es|ed)?|crushes|crushed|smashes|smashed) (?:\w+ ){0,3}" + _EST, 2.5),
    (r"\b(?:miss(?:es|ed)?|trails?|trailed|falls? short of|fell short of|lags?|lagged) (?:\w+ ){0,3}" + _EST, -2.5),
    (r"\b(?:above|ahead of|better than) (?:\w+ ){0,1}" + _EST + r"|\bbetter[- ]than[- ]expected\b|\bstronger[- ]than[- ]expected\b", 2.0),
    (r"\bbelow (?:\w+ ){0,1}" + _EST + r"|\bworse[- ]than[- ]expected\b|\bweaker[- ]than[- ]expected\b", -2.0),
    (r"\b" + _UPV + r" " + _GQ + _GW, 3.0),
    (r"\b" + _DNV + r" " + _GQ + _GW, -3.0),
    (r"\b(?:weak|weaker|disappointing|soft|softer|downbeat|bleak|gloomy|dismal|tepid|lackluster) (?:\w+ ){0,2}" + _GW, -2.5),
    (r"\b(?:strong|upbeat|rosy|bullish|robust|blowout|better) (?:\w+ ){0,2}" + _GW, 2.0),
    (r"\bprofit warning\b", -3.0),
    (r"\bgoing concern\b", -4.0),
    (r"\b(?:files?|filed|filing) (?:\w+ ){0,2}(?:for )?(?:bankruptcy|chapter 11)\b|\bchapter 11\b", -4.0),
    (r"\bfda (?:approv\w*|clear\w*|grants?|granted|authoriz\w*|nod)\b|\b(?:approved|cleared) by (?:the )?(?:u\.s\. )?fda\b|\b(?:receives?|received|wins?|won|gets?|secures?|granted) (?:u\.s\. )?fda (?:approval|clearance|nod)\b", 3.0),
    (r"\b(?:lifts?|lifted|removes?|removed|clears?|cleared|ends?|ended) (?:the |a |its )?(?:partial |full )?clinical hold\b|\bclinical hold (?:lifted|removed)\b", 2.5),
    (r"\bfda (?:rejects?|rejected|declines?|declined)\b|\bcomplete response letter\b|\bclinical hold\b", -3.5),
    (r"\b(?:breakthrough therapy|fast track|orphan drug|priority review) designation\b", 1.5),
    (r"\b(?:met|meets|achieved|achieves) (?:\w+ ){0,3}primary endpoints?\b|\bpositive (?:\w+ ){0,2}(?:topline|top-line|phase|trial|interim) (?:\w+ ){0,2}(?:results|data)\b|\bstatistically significant\b", 2.5),
    (r"\b(?:fail(?:s|ed)? to meet|did not meet|missed|misses) (?:\w+ ){0,3}(?:primary )?endpoints?\b|\b(?:trial|study) (?:failed|fails|halted|stopped)\b", -3.5),
    (r"\bupgrade[sd]? (?:\w+ ){0,4}to (?:buy|outperform|overweight|strong buy)\b", 2.5),
    (r"\bdowngrade[sd]? (?:\w+ ){0,4}to (?:sell|underperform|underweight|neutral|hold|equal[- ]weight|market perform|sector perform)\b", -2.5),
    (r"\b" + _UPV + r" (?:\w+ ){0,4}price (?:target|objective)\b|\bprice target (?:raised|lifted|boosted|hiked|increased)\b", 1.5),
    (r"\b" + _DNV + r" (?:\w+ ){0,4}price (?:target|objective)\b|\bprice target (?:cut|lowered|slashed|trimmed|reduced)\b", -1.5),
    (r"\binitiat\w* (?:coverage )?(?:\w+ ){0,4}(?:at|with)(?: an?)? (?:buy|outperform|overweight|strong buy)\b", 1.5),
    (r"\binitiat\w* (?:coverage )?(?:\w+ ){0,4}(?:at|with)(?: an?)? (?:sell|underperform|underweight)\b", -1.5),
    (r"\b(?:record|all-time|52-week|multi-?year) highs?\b", 1.5),
    (r"\b(?:record|52-week|all-time|multi-?year|\d+-(?:year|month)|(?:one|two|three|five|six|ten)-(?:year|month)) lows?\b", -1.5),
    (r"\b(?:wholesale|consumer|producer|import|gas|gasoline|food|energy|electricity|grocery|egg|fuel) prices (?:\w+ ){0,1}(?:skyrocket|soar|surge|jump|spike|climb|rise|rose|accelerat)\w*", -1.5),
    (r"\b(?:announces?|authoriz\w*|approves?|unveils?|launches?|boosts?|expands?|new) (?:\w+ ){0,4}(?:buyback|share repurchase|repurchase program|stock repurchase)\b", 1.5),
    (r"\b(?:raises?|raised|increases?|increased|boosts?|boosted|hikes?|hiked) (?:\w+ ){0,2}dividend\b|\bspecial dividend\b|\bdividend (?:hike|increase)\b", 1.5),
    (r"\b(?:cuts?|slashes?|slashed|suspends?|suspended|eliminates?|eliminated|omits?) (?:\w+ ){0,2}dividend\b|\bdividend (?:cut|suspension)\b", -2.5),
    (r"\b(?:wins?|won|awarded|secures?|secured|lands?|landed) (?:\w+ ){0,4}(?:contract|order)s?\b", 1.5),
    (r"\binsider (?:buying|purchase)\b|\b(?:ceo|cfo|director|chairman|insider|founder) (?:\w+ ){0,2}(?:buys|bought|purchases|purchased)\b", 1.2),
    (r"\binsider selling\b|\b(?:ceo|cfo|director|chairman|insider|founder) (?:\w+ ){0,2}(?:sells|sold|dumps|unloads|offloads)\b", -0.8),
    (r"\b(?:ceo|cfo|chief executive) (?:\w+ ){0,2}(?:resigns?|resigned|ousted|fired|abruptly)\b", -1.5),
    (r"\b(?:public|secondary|follow-on|registered direct|proposed|underwritten|best[- ]efforts|stock|share|common stock|equity) offering\b|\bpriced (?:\w+ ){0,4}offering\b|\bprivate placement\b", -1.5),
    (r"\bat[- ]the[- ]market\b|\bshelf registration\b", -1.0),
    (r"\breverse (?:stock |share )?split\b", -2.0),
    (r"\b(?:stock|share) split\b", 1.2),
    (r"\b(?:minimum bid price|delisting notice|notice of delisting|non-?compliance notice)\b|\b(?:nasdaq|nyse) (?:deficiency|delisting)\b", -2.5),
    (r"\bregain\w* compliance\b", 1.5),
    (r"\bshort[- ]sellers?\b|\bshort (?:report|attack)\b", -2.0),
    (r"\b(?:sec|doj|ftc|justice department|prosecutors|regulators) (?:\w+ ){0,2}(?:probe|investigat\w+|charges?|charged|sues?|sued|subpoena\w*)\b", -2.0),
    (r"\bclass[- ]action\b|\bsecurities fraud\b|\bantitrust (?:lawsuit|suit|probe|case)\b", -1.5),
    (r"\b(?:rate|rates) cuts?\b|\b(?:cuts?|cutting|lowers?|lowered|slashes?) (?:\w+ ){0,2}(?:interest )?rates\b|\bdovish\b|\bsoft landing\b", 1.0),
    (r"\b(?:rate|rates) hikes?\b|\b(?:raises?|raised|raising|hikes?|hiked) (?:\w+ ){0,2}(?:interest )?rates\b|\bhawkish\b", -1.0),
    (r"\b(?:hotter|higher)[- ]than[- ]expected (?:inflation|cpi|pce|prices)\b|\binflation (?:accelerates|heats up|jumps|surges|rises|rose)\b", -1.5),
    (r"\b(?:consumer |producer |import )?(?:prices|inflation|cpi|pce|ppi) (?:\w+ ){0,2}(?:rise|rises|rose|jump\w*|climb\w*|accelerat\w*|heat\w*|increas\w*|surg\w*)(?: \w+){0,2} (?:more|faster) than (?:expected|forecast)\b", -2.0),
    (r"\b(?:consumer |producer )prices (?:rise|rises|rose|jump\w*|climb\w*)\b|\btariffs? (?:bite|hit|weigh)\w*\b", -1.0),
    (r"\b(?:cooler|softer|lower)[- ]than[- ]expected (?:inflation|cpi|pce|prices)\b|\binflation (?:cools|eases|slows)\b", 1.5),
    (r"\b(?:to be acquired|agrees? to be (?:acquired|bought)|takeover (?:bid|offer|approach)|buyout offer|premium to)\b", 2.0),
    (r"\b(?:cuts?|cutting|slash\w*|eliminat\w*) (?:about |nearly |roughly |some |over )?(?:\d[\d,]* )?(?:jobs|positions|roles|workers|employees)\b|\b(?:job cuts|layoffs)\b", -0.8),
    (r"\b(?:raises?|raised|raising) \$[\d.,]+ ?(?:million|mln|m)\b", -0.5),
    (r"\bshort squeeze\b", 1.0),
    (r"\bactivist (?:\w+ ){0,3}(?:stake|investor|campaign)\b|\b(?:takes?|took|builds?|built|discloses?|disclosed) (?:an? )?(?:\$?[\d.,]+ ?(?:million|billion|%)? )?(?:\w+ ){0,2}stake in\b", 1.5),
    (r"\bauditor (?:\w+ ){0,2}(?:resign\w*|quits?|dismiss\w*)\b|\bmaterial weakness\b|\baccounting (?:irregularities|probe|scandal)\b|\brestate\w*\b", -3.0),
    (r"\b(?:trading halt|halted|trading suspended)\b", -0.3),
    (r"\b(?:to join|added to|to be added to|set to join) (?:the )?(?:s&p (?:500|midcap 400|smallcap 600)|nasdaq[- ]100)\b", 1.5),
    (r"\bin line with (?:\w+ ){0,1}" + _EST + r"\b|\bmixed (?:results|quarter|report)\b", 0.0),
    # Index / share moves with a number ("Nasdaq futures down 1.8%", "Meta shares are down 9% this week").
    (r"\b(?:shares?|stocks?|futures|dow|nasdaq|s&p 500|s&p|russell 2000|composite|equities)\b(?:\s+[\w&'.\-]+){0,3}?\s+(?:down|lower|off)\s+\d+(?:\.\d+)?%", -1.2),
    (r"\b(?:shares?|stocks?|futures|dow|nasdaq|s&p 500|s&p|russell 2000|composite|equities)\b(?:\s+[\w&'.\-]+){0,3}?\s+up\s+\d+(?:\.\d+)?%", 1.2),
    (r"\bdown \d+(?:\.\d+)?(?:%| percent| points?)", -0.4),
    (r"\bup \d+(?:\.\d+)?%", 0.3),
    # Cost / commodity / yield up-moves are not good news for stocks: neutralise the positive verb ("oil surges").
    (r"\b(?:oil|crude|brent|wti|gas(?:oline)?|fuel|yields?|inflation|prices|costs?|fares?|rates)\s+(?:(?:oil|crude|prices?|futures|costs?|yields?)\s+)?(?:surg|soar|jump|spik|rall|climb|rise|rising|rose|top|hit)\w*", 0.0),
    (r"\bprices? (?:surge|jump|rise|soar)\w* (?:the )?most since\b", -1.0),
    (r"\b(?:largest|biggest|highest|fastest) (?:\d+-month |annual |monthly |yearly )?(?:increase|rise|jump) since\b", -1.0),
]

_WORD_RX = re.compile(r"[a-z][a-z'\-]*[a-z]|[a-z]")
_WINDOWS = {n: re.compile(r"\b(?=(\w.{%d}))" % (n - 1), re.S) for n in range(1, 5)}
_LEX: dict[str, float] = {}
_NEG_BEFORE = re.compile(r"(?:\b(?:not|no|never|without|didn't|doesn't|don't|won't|isn't|wasn't|fails? to|failed to|unable to)\s+(?:\w+\s+){0,1})$")
_DEAL = re.compile(r"\$\s?\d[\d.,]*\s?(?:billion|bln|bn|b|trillion|tn)\b", re.I)
_BIGPCT = re.compile(r"(?<![\d.])(\d{2,3})(?:\.\d+)?%")


class _RuleIndex:
    """Runs only the regexes that can possibly match: each rule's possible 1-4 char match prefixes are
    derived from its parse tree, looked up at word starts of the lowercased text, and the rule is then
    tried only at those positions (rules without a usable prefix fall back to a full search)."""

    L = 4

    def __init__(self, patterns: list[str]):
        self.rx = [re.compile(p, re.I) for p in patterns]
        self.by_prefix: dict[str, list[int]] = {}
        self.substr: list[tuple[str, int]] = []
        self.always: list[int] = []
        for i, p in enumerate(patterns):
            pref = self._prefixes(p)
            if not pref:
                self.always.append(i)
                continue
            for x in pref:
                if x[0].isalnum() or x[0] == "_":
                    self.by_prefix.setdefault(x, []).append(i)
                else:
                    self.substr.append((x, i))
        self.keys = {n: {k for k in self.by_prefix if len(k) == n} for n in range(1, self.L + 1)}

    @classmethod
    def _prefixes(cls, pattern: str) -> set[str] | None:
        try:
            from re import _parser  # type: ignore[attr-defined]
            from re._constants import (ANY, ASSERT, ASSERT_NOT, AT, BRANCH, IN, LITERAL, MAX_REPEAT,
                                       MIN_REPEAT, RANGE, SUBPATTERN)
        except ImportError:
            return None
        L = cls.L
        zero = {("", True)}

        def concat(a, b):
            out = set()
            for x, xo in a:
                if not xo or len(x) >= L:
                    out.add((x[:L], False))
                    continue
                for y, yo in b:
                    z = x + y
                    out.add((z[:L], False) if len(z) >= L else (z, yo))
            return out

        def item(op, av):
            if op == LITERAL:
                return {(chr(av).lower(), True)}
            if op in (AT, ASSERT, ASSERT_NOT):
                return zero
            if op == IN:
                chars = set()
                for o, a in av:
                    if o == LITERAL:
                        chars.add(chr(a).lower())
                    elif o == RANGE and a[1] - a[0] < 12:
                        chars |= {chr(c).lower() for c in range(a[0], a[1] + 1)}
                    else:
                        return None
                return {(c, True) for c in chars}
            if op == SUBPATTERN:
                return seq(av[-1])
            if op == BRANCH:
                out = set()
                for alt in av[1]:
                    r = seq(alt)
                    if r is None:
                        return None
                    out |= r
                return out
            if op in (MAX_REPEAT, MIN_REPEAT):
                mn, mx, sub = av
                body = seq(sub)
                if body is None:
                    return None
                out = set(zero) if mn == 0 else set()
                cur = zero
                for k in range(1, min(mx, L) + 1):
                    cur = concat(cur, body)
                    if k >= mn or k == L:
                        out |= cur
                return out
            if op == ANY:
                return None
            return None

        def seq(items):
            res = zero
            for op, av in items:
                if all((not o) or len(x) >= L for x, o in res):
                    break
                opts = item(op, av)
                if opts is None:
                    res = {(x, False) for x, _ in res}
                    break
                res = concat(res, opts)
                if len(res) > 400:
                    return None
            return res

        try:
            res = seq(_parser.parse(pattern, 0))
        except Exception:  # noqa: BLE001
            return None
        if res is None or any(x == "" for x, _ in res):
            return None
        return {x for x, _ in res}

    def scan(self, low: str) -> dict[int, list[int] | None]:
        """{rule id: sorted start positions to try, or None = search the whole text}."""
        out: dict[int, list[int] | None] = {i: None for i in self.always}
        hits: set[str] = set()
        for n, keys in self.keys.items():
            if keys:
                hits |= keys.intersection(_WINDOWS[n].findall(low))
        for key in hits:
            pos = []
            j = low.find(key)
            while j >= 0:
                if j == 0 or not (low[j - 1].isalnum() or low[j - 1] == "_"):
                    pos.append(j)
                j = low.find(key, j + 1)
            if not pos:
                continue
            for r in self.by_prefix[key]:
                if r not in out:
                    out[r] = list(pos)
                elif out[r] is not None:
                    out[r].extend(pos)
        for x, r in self.substr:
            if x in low:
                out[r] = None
        for r, v in out.items():
            if v is not None and len(v) > 1:
                out[r] = sorted(set(v))
        return out


_BUILD_LOCK = threading.Lock()
_INDEX: _RuleIndex | None = None
_CAT_META: list[tuple[str, float]] = []   # rule id -> (category, factor); ids >= _NCAT are phrases
_CAT_GUARD: dict[int, object] = {}        # rule id -> guard(text, match) -> True when the match must be ignored
_NCAT = 0

# Macro data releases: company-earnings wording ("Exceeds Expectations", "Weekly Earnings") means nothing there.
_MACRO_TXT = re.compile(
    r"\b(?:pmi|ism|cpi|ppi|pce|gdp|(?:business|factory|manufacturing|services|economic) activity|jobless claims|"
    r"nonfarm|non-farm|payrolls|jobs report|job growth|retail sales|consumer (?:confidence|sentiment|spending|prices)|"
    r"producer prices|price index|housing starts|home sales|durable goods|industrial production|unemployment rate|"
    r"(?:weekly|hourly|average) earnings|wage growth|economic data)\b", re.I)
# "Trump's approval rating hits all-time low", "Dollar Index Falls 0.25%": not a stock's price move.
_NON_EQUITY = re.compile(
    r"\b(?:approval rating|activity|growth|sentiment|confidence|dollar|yen|euro|yuan|yields?|treasur\w*|inflation|"
    r"prices|pmi|rates|ipc|index(?! fund))\b", re.I)
_RX_WORDS_BEFORE = re.compile(r"(?:\S+\s+){0,6}$")
_IPO_HIST = re.compile(r"\bsince (?:its |their |the )?(?:\w+ ){0,2}$|\bits (?:\w+ )?$|\bpost-$", re.I)


def _guard_macro(text: str, m: re.Match) -> bool:
    return bool(_MACRO_TXT.search(text))


def _guard_nonequity(text: str, m: re.Match) -> bool:
    w = _RX_WORDS_BEFORE.search(text, max(0, m.start() - 80), m.start())
    return bool(w and _NON_EQUITY.search(w.group(0)))


def _guard_ipo_hist(text: str, m: re.Match) -> bool:
    """'Since Its IPO', 'Since Its April IPO', 'its IPO', 'post-IPO': history, not an IPO event."""
    return bool(_IPO_HIST.search(text, max(0, m.start() - 40), m.start()))


def _guard_fund(text: str, m: re.Match) -> bool:
    """Fund / ETF holdings wording: "ARK buys Freenome shares" is a position, not an acquisition."""
    return bool(_FUND_TXT.search(text))


_GUARDS = {"macro": _guard_macro, "nonequity": _guard_nonequity, "ipo_hist": _guard_ipo_hist, "fund": _guard_fund}

# SPAC names ("SIM Acquisition Corp. I") must not read as M&A / IPO news; masked before matching.
_SPAC_NAME = re.compile(
    r"[A-Z][\w&.]*(?: [A-Z][\w&.]*){0,5} (?:Acquisition|ACQUISITION|SPAC|Tech SPAC)(?: Partners| PARTNERS)? "
    r"(?:Corp\.?|CORP\.?|Corporation|CORPORATION|Co\.|Company|Holdings|HOLDINGS|Ltd\.?|Limited)(?: [IVX]+\b)?")
_CONCESSIVE = re.compile(r"\b(?:despite|even as|although|though)\b")


def _mask_entities(text: str) -> str:
    if "cquisition" not in text and "CQUISITION" not in text and "SPAC" not in text:
        return text
    return _SPAC_NAME.sub(lambda m: " " * len(m.group(0)), text)


def _inflect(word: str) -> set[str]:
    out = {word, word + "s", word + "es", word + "ed", word + "d", word + "ing"}
    if word.endswith("e"):
        out.add(word[:-1] + "ing")
    if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        out |= {word[:-1] + "ies", word[:-1] + "ied"}
    if len(word) >= 3 and word[-1] not in "aeiouywx" and word[-2] in "aeiou" and word[-3] not in "aeiou":
        out |= {word + word[-1] + "ed", word + word[-1] + "ing"}
    return out


def _build() -> None:
    global _INDEX, _NCAT
    if _INDEX is not None:
        return
    with _BUILD_LOCK:
        if _INDEX is not None:
            return
        pats = []
        for key, rules in _RULES.items():
            for p, f, *guard in rules:
                if guard:
                    _CAT_GUARD[len(pats)] = _GUARDS[guard[0]]
                pats.append(p)
                _CAT_META.append((key, f))
        _NCAT = len(pats)
        pats += [p for p, _ in _PHRASES]
        for words, sign in ((_NEG_WORDS, -1.0), (_POS_WORDS, 1.0)):
            for w, v in words.items():
                for f in _inflect(w):
                    _LEX.setdefault(f, sign * v)
                _LEX[w] = sign * v
        _INDEX = _RuleIndex(pats)


def _features(text: str, title: bool = False) -> tuple[dict[str, float], float]:
    """One pass over a text: ({category: best rule factor}, raw lexicon sentiment). `title` enables the
    contrast-clause weighting ("X beats, but Y falls": the clause after "but" carries the outcome)."""
    cats: dict[str, float] = {}
    if not text:
        return cats, 0.0
    _build()
    idx = _INDEX
    text = _mask_entities(text).replace("’", "'")
    text = _NONCOMMON_PT.sub(" ", text)
    low = text.lower()
    exact = len(low) == len(text)
    spans = []
    rxs = idx.rx
    for rid, where in idx.scan(low).items():
        rx = rxs[rid]
        if rid < _NCAT:
            key, f = _CAT_META[rid]
            if f <= cats.get(key, 0.0):
                continue
            guard = _CAT_GUARD.get(rid)
            if where is None or not exact:
                if guard is None:
                    ok = rx.search(text) is not None
                else:
                    ok = any(not guard(text, m) for m in rx.finditer(text))
            else:
                ok = False
                for pos in where:
                    m = rx.match(text, pos)
                    if m and (guard is None or not guard(text, m)):
                        ok = True
                        break
            if ok:
                cats[key] = f
        else:
            w = _PHRASES[rid - _NCAT][1]
            found = rx.finditer(low) if where is None else (rx.match(low, pos) for pos in where)
            for m in found:
                if m and m.end() > m.start():
                    spans.append((m.start(), m.start() - m.end(), m.end(), w))
    # Words / phrases in a concessive clause ("... despite strong results") count 30%.
    c = _CONCESSIVE.search(low)
    cpos = c.start() if c else len(low) + 1
    k = _CONTRAST.search(low) if title else None
    kpos = k.start() if k else None

    def _wf(p: int) -> float:
        # After "despite/although" 30%; before a title's "but/yet/however" 30% (the later clause decides).
        return 0.3 if p >= cpos or (kpos is not None and p < kpos) else 1.0

    total = 0.0
    pieces: list[tuple[int, str]] = []
    last = 0
    for st, _, en, w in sorted(spans):
        if st < last:
            continue
        if _NEG_BEFORE.search(low, max(0, st - 24), st):
            w = -w * 0.8
        total += w * _wf(st)
        pieces.append((last, low[last:st]))
        last = en
    pieces.append((last, low[last:]))
    neg_until = -1
    lex = _LEX
    i = 0
    for off, piece in pieces:
        for wm in _WORD_RX.finditer(piece):
            w = wm.group(0)
            if w in _NEGATORS or w.endswith("n't"):
                neg_until = i + 3
                i += 1
                continue
            v = lex.get(w)
            if v is not None:
                v = -v * 0.8 if i <= neg_until else v
                total += v * _wf(off + wm.start())
            i += 1
    return cats, total


def _combine_scores(tc: dict[str, float], sc: dict[str, float], title: str = "") -> dict[str, float]:
    out = {k: CATEGORIES[k]["weight"] * f for k, f in tc.items()}
    analyst_ok = bool(_ANALYST_WORD.search(title or ""))
    for k, f in sc.items():
        if k in out:
            continue
        if k in ("analyst_up", "analyst_down") and not analyst_ok:
            continue                       # summary-only analyst match without an analyst word in the title
        if k == "ai":
            f = min(f, 0.3)                # AI in a summary is weak evidence unless the title says so
        out[k] = CATEGORIES[k]["weight"] * f * 0.5
    return out


_RX_TITLE_MOVE = re.compile(
    r"\b(?:soar|surg|jump|rall|climb|spik|rocket|skyrocket|plung|plummet|tumbl|sink|sank|slump|crater|tank|crash|slid|"
    r"slip|fall|fell|drop|rise|rose|gain)\w*|\b(?:up|down) \d", re.I)


def _combine_sentiment(t_raw: float, s_raw: float, extra: dict | None, title_move: bool = False) -> float:
    s_raw = max(-5.0, min(5.0, s_raw))
    if title_move and t_raw:
        # The title says how the stock moved; the summary must not flip that sign.
        cap = abs(2.0 * t_raw)
        s_raw = max(-cap, min(cap, s_raw))
    s = math.tanh((2.0 * t_raw + s_raw) / 5.0)
    src = _source_sentiment(extra)
    if src is not None:
        s = 0.75 * s + 0.45 * src
    return round(max(-1.0, min(1.0, s)), 3)


# ---------------------------------------------------------------- public helpers

_QUOTES = {0x2019: "'", 0x2018: "'", 0x201c: '"', 0x201d: '"'}


def _q(text: str) -> str:
    return (text or "").translate(_QUOTES)


def features(text: str, title: bool = False) -> tuple[dict[str, float], float]:
    """({category: best rule factor}, raw lexicon sentiment) for one text."""
    return _features(text, title)


def category_scores(title: str, summary: str = "") -> dict[str, float]:
    """{category: strength} where strength = weight x rule factor (summary-only matches count half)."""
    return _combine_scores(_features(title, True)[0], _features(summary)[0], title)


def classify(title: str, summary: str = "") -> list[str]:
    """Category keys, strongest first (title matches before summary-only matches)."""
    scores = category_scores(title, summary)
    if is_opinion(title):
        scores["opinion"] = 0.0
    return sorted(scores, key=lambda k: -scores[k])


def is_opinion(title: str, publisher: str = "") -> bool:
    return bool(_OPINION.search(_q(title)))


def is_lawfirm_spam(title: str, summary: str = "") -> bool:
    head = (summary or "")[:150]
    return bool(_LAWFIRM.search(title or "") or _LAWFIRM.search((summary or "")[:300])
                or _LAWFIRM_OPEN.search(head) or _LAWFIRM_OPEN.search(title or ""))


# Option-chain / quote / chart pages that feeds publish as "news".
_JUNK = re.compile(
    r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b|\b(?:call|put) \([A-Z]{1,5}\d{6}|Stock Price & Latest News|"
    r"Stock Price, News, Quote & History|Interactive Stock Chart|"
    r"is (?:up|down) [\d.]+% today, on [A-Z.]+ stock price movement|Financial Statements [\u2013-] (?:NASDAQ|NYSE)|"
    r"Stock Price, News & Analysis|Price, Quote & Chart|Stock Forum and Discussion|Stock Options Chain", re.I)


def is_junk_title(title: str) -> bool:
    """True for quote / option-chain / chart pages that are not news."""
    return bool(title and _JUNK.search(title))


def _flags(title: str, summary: str) -> dict:
    return {"opinion": is_opinion(title), "spam": is_lawfirm_spam(title, summary),
            "analysis": bool(_ANALYSIS.search(_q(title))), "junk": is_junk_title(title)}


# Market wraps / digests / movers lists: catalysts in them belong to many companies, not to one ticker.
_ROUNDUP = re.compile(
    r"\b(?:stock market today|markets? wrap|market (?:recap|wrap)|stocks? making (?:the )?(?:biggest )?moves|"
    r"biggest (?:moves|movers)|(?:top|premarket|pre-market|midday|after-hours|stock) movers|movers:|what to watch|"
    r"things to (?:know|watch)|catalyst watch|week ahead|(?:morning|evening|daily|weekly) (?:brief|briefing|digest|recap)|"
    r"newsletter|roundup|round-up|sell-?off (?:continues|deepens|spreads|resumes)|stocks? (?:to watch|on the move))\b",
    re.I)


def is_roundup(title: str, title_cats: dict[str, float] | None = None) -> bool:
    if _ROUNDUP.search(_q(title)):
        return True
    tc = title_cats if title_cats is not None else _features(title)[0]
    return tc.get("market", 0.0) >= 0.8


# ---------------------------------------------------------------- event outcomes (from the text, never from tone)
_PREVIEW = re.compile(r"\b(?:ahead of|looms?|preview|to report|set to report|poised|what to expect|expectations for|"
                      r"on deck|days away|upcoming|to focus on|will report|week ahead)\b", re.I)
_DONE = re.compile(r"\b(?:completes?|completed|completion of|consummat\w*|(?:stock|share)holders approve\w*|"
                   r"approve[sd]? (?:the )?merger)\b|\bclos(?:es|ed|ing) (?:its |the )?(?:\$[\d.,]+\s*[bm]\w*\s+)?"
                   r"(?:acquisition|merger|deal|purchase|sale|transaction)\b", re.I)
_EQUITY_SUBJ = re.compile(r"\b(?:shares?|stocks?)\b", re.I)
_BEAT = re.compile(
    r"\b(?:beats?|tops?|topped|exceeds?|exceeded|surpass\w*|crush\w*|edged above|above|ahead of)\b(?:\s+[\w\-]+){0,3}?"
    r"\s+(?:estimates|expectations|consensus|views|forecasts?|projections)\b|"
    r"\b(?:profit|revenue|sales|eps|earnings|quarterly|q[1-4]) beats?\b|\bbeats? on (?:the )?(?:top|bottom)\b|"
    r"\bbeat[- ](?:and|&)[- ]raise\b|\bbetter[- ]than[- ]expected (?:results|earnings|profit|revenue|quarter|sales)\b",
    re.I)
_MISS = re.compile(
    r"\b(?:miss(?:es|ed)?|falls? short of|fell short of|trails?|trailed|lags?|lagged|below|under)\b(?:\s+[\w\-]+){0,3}?"
    r"\s+(?:estimates|expectations|consensus|views|forecasts?|projections)\b|"
    r"\b(?:profit|revenue|sales|eps|earnings|quarterly|q[1-4]) miss(?:es)?\b|\bmiss(?:es|ed)? on (?:the )?(?:top|bottom)\b|"
    r"\bworse[- ]than[- ]expected (?:results|earnings|profit|revenue|quarter|sales)\b", re.I)
_NOT_BEAT = re.compile(r"\b(?:fail(?:s|ed)? to|did ?n[o']t|does ?n[o']t|not|unable to) (?:beat|top|exceed|surpass|meet)\b", re.I)
_FDA_OK = re.compile(
    r"\b(?:fda|agency) (?:\w+ ){0,2}(?:approves|approved|clears|cleared|authorizes|authorized|grants?|granted)\b|"
    r"\b(?:approved|cleared|authorized) by (?:the )?(?:u\.s\. )?(?:fda|food and drug administration)\b|"
    r"\b(?:receives?|received|wins?|won|gets?|got|secures?|secured|obtains?|obtained|granted|earns?|earned)\b"
    r"(?:\s+[\w.\-]+){0,4}?\s+(?:approval|clearance|authorization|nod|green light)\b", re.I)
_FDA_NO = re.compile(
    r"\b(?:fda|agency|regulators?) (?:\w+ ){0,2}(?:rejects?|rejected|declines?|declined|refuses?|refused|denies|denied)\b|"
    r"\b(?:rejected|denied) by (?:the )?(?:u\.s\. )?(?:fda|food and drug administration)\b|"
    r"\bcomplete response letter\b|(?-i:\bCRL\b)|\bclinical hold\b", re.I)
_DIV_UP = re.compile(
    r"\b(?:raises?|raised|increases?|increased|hikes?|hiked|boosts?|boosted|lifts?|lifted)\b(?:\s+\w+){0,3}?\s+"
    r"(?:dividend|payout|distribution)s?\b|\b(?:dividend|payout) (?:hike|increase|raise|boost)\b|\bspecial dividend\b",
    re.I)
_DIV_DN = re.compile(
    r"\b(?:cuts?|slashes?|slashed|suspends?|suspended|eliminates?|eliminated|omits?|omitted|reduces?|reduced|halts?|"
    r"halted)\b(?:\s+\w+){0,3}?\s+(?:dividend|payout|distribution)s?\b|\b(?:dividend|payout) (?:cut|suspension|reduction)\b",
    re.I)
_INS_BUY = re.compile(r"\b(?:buys?|bought|buying|purchases?|purchased|purchasing|loads? up|loaded up|adds?|added)\b"
                      r"|(?-i:\bALIM\b)", re.I)
_INS_SELL = re.compile(r"\b(?:sells?|sold|selling|sales?|dispos\w*|dumps?|dumped|unloads?|unloaded|offloads?|"
                       r"offloaded|trims?|trimmed)\b|(?-i:\bSATIŞ\b)", re.I)
_CLIN_POS = re.compile(
    r"\b(?:met|meets|achieved|achieves|hits?) (?:\w+ ){0,3}(?:primary |secondary )?endpoints?\b|"
    r"\bpositive (?:\w+ ){0,3}(?:topline|top-line|interim|phase|trial|study|clinical|results|data)\b|"
    r"\bstatistically significant\b|\bencouraging (?:\w+ ){0,2}(?:results|data)\b", re.I)
_CLIN_NEG = re.compile(
    r"\b(?:fail(?:s|ed)? to (?:meet|achieve|hit)|did not meet|missed|misses|miss) (?:\w+ ){0,3}(?:primary |secondary )?"
    r"endpoints?\b|\b(?:trial|study) (?:failed|fails|halted|stopped|discontinued)\b|"
    r"\bnegative (?:\w+ ){0,3}(?:results|data|topline|readout)\b|\bdisappointing (?:\w+ ){0,2}(?:results|data|readout)\b|"
    r"\bclinical hold\b", re.I)
_DELIST_OK = re.compile(r"\bregain\w* compliance\b", re.I)
_INDEX_OUT = re.compile(r"\b(?:removed from|to be removed from|dropped from|deleted from|exits?|leaves?|to leave)\s+"
                        r"(?:the\s+)?(?:s&p|nasdaq[- ]100|dow jones|russell)", re.I)


def _two_way(pos: re.Pattern, neg: re.Pattern, a: str, b: str):
    def fn(text: str) -> str | None:
        p, n = bool(pos.search(text)), bool(neg.search(text))
        return "mixed" if p and n else a if p else b if n else None
    return fn


def _earnings_outcome(text: str) -> str | None:
    if not text or _MACRO_TXT.search(text):
        return None
    neg = _NOT_BEAT.search(text)
    t = _NOT_BEAT.sub(" ", text) if neg else text
    b, m = bool(_BEAT.search(t)), bool(neg or _MISS.search(t))
    return "mixed" if b and m else "beat" if b else "miss" if m else None


_OUTCOME_FNS = {
    "earnings": _earnings_outcome,
    "fda": _two_way(_FDA_OK, _FDA_NO, "approve", "reject"),
    "dividend": _two_way(_DIV_UP, _DIV_DN, "raise", "cut"),
    "insider": _two_way(_INS_BUY, _INS_SELL, "buy", "sell"),
    "clinical": _two_way(_CLIN_POS, _CLIN_NEG, "positive", "negative"),
    "delisting": lambda text: "compliance" if _DELIST_OK.search(text) else None,
    "index_change": lambda text: "removed" if _INDEX_OUT.search(text) else None,
}


def outcomes(title: str, summary: str = "", categories=(), extra: dict | None = None) -> dict[str, str]:
    """What actually happened for outcome-bearing categories, read from the wording (title first, then summary):
    earnings beat|miss|mixed|preview, fda approve|reject, dividend raise|cut, insider buy|sell, clinical
    positive|negative, delisting compliance, index_change removed. Categories without explicit wording are left out."""
    title, summary = _q(title), _q(summary)
    out: dict[str, str] = {}
    for cat in categories or ():
        fn = _OUTCOME_FNS.get(cat)
        if fn is None or cat in out:
            continue
        o = None
        if cat == "insider" and isinstance(extra, dict) and extra.get("direction") in ("buy", "sell", "mixed"):
            o = extra["direction"]
        o = o or fn(title) or fn(summary)
        if cat == "earnings" and o is None and _PREVIEW.search(title):
            o = "preview"
        if o:
            out[cat] = o
    return out


def _source_sentiment(extra: dict | None) -> float | None:
    if not isinstance(extra, dict):
        return None
    v = extra.get("sentiment")
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return max(-1.0, min(1.0, float(v)))
    s = str(v).strip().lower()
    if s in ("bullish", "pos", "positive", "olumlu", "long"):
        return 1.0
    if s in ("bearish", "neg", "negative", "olumsuz", "short"):
        return -1.0
    return None


def sentiment(title: str, summary: str = "", extra: dict | None = None) -> float:
    """Lexicon sentiment in -1..1 (title counts double, summary contribution capped; StockTwits-style
    extra.sentiment Bullish/Bearish or a numeric source score is blended in)."""
    return _combine_sentiment(_features(title, True)[1], _features(summary)[1], extra,
                              bool(_RX_TITLE_MOVE.search(title or "")))


def sentiment_label(s: float, band: float = 0.15) -> str:
    return "pos" if s >= band else "neg" if s <= -band else "neu"


MEGA_CAPS = set("AAPL MSFT NVDA AMZN GOOGL GOOG META TSLA AVGO BRK.B LLY JPM V WMT ORCL MA NFLX XOM COST UNH "
                "PLTR AMD TSM".split())
_CATALYSTS = {"index_change", "accounting", "earnings", "guidance_up", "guidance_down", "mna", "fda", "clinical", "offering",
              "buyback", "contract", "bankruptcy", "halt", "short_report", "split", "reverse_split", "delisting",
              "insider", "management", "activist", "analyst_up", "analyst_down", "ipo", "dividend", "lawsuit"}
_MARKET_WIDE = {"fed", "macro", "geopolitics", "market", "commodities"}


def importance(item: dict, cfg: dict | None = None, scores: dict[str, float] | None = None,
               flags: dict | None = None) -> float:
    """0..100 newsworthiness for a day trader. `scores` = category_scores() if already computed;
    `flags` = {"opinion": bool, "spam": bool} if already computed. Ticker bonuses go to the item's subject:
    `extra.title_tickers` (tickers named in the title), else its only ticker."""
    title = item.get("title") or ""
    summary = item.get("summary") or ""
    if scores is None:
        scores = category_scores(title, summary)
    if flags is None:
        flags = _flags(title, summary)
    tickers = item.get("tickers") or []
    stype = item.get("source_type") or "news"
    s = float(item.get("sentiment") or 0.0)
    watch = set((cfg or {}).get("watchlist") or [])
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    tt = extra.get("title_tickers")
    if isinstance(tt, list):
        subj = list(tt) or (list(tickers) if len(tickers) == 1 else [])
    else:
        subj = list(tickers)  # caller without title analysis

    sc = {k: v for k, v in scores.items() if k != "opinion"}
    top0 = max(sc, key=sc.get) if sc else ""
    if top0 == "earnings" and _PREVIEW.search(title):
        sc["earnings"] *= 0.45   # "... As Earnings Report Looms": the event has not happened yet
    if "mna" in sc and _DONE.search(title):
        sc["mna"] *= 0.45        # completed / approved deal: paperwork, not a new catalyst
    m = _BIGPCT.search(title)
    pct = int(m.group(1)) if m else 0
    big_move = pct >= 10 and "price_move" in sc and bool(subj or _EQUITY_SUBJ.search(title))
    if big_move:
        sc["price_move"] = max(sc["price_move"], CATEGORIES["price_move"]["weight"] * (2.5 if pct >= 20 else 2.0))

    ranked = sorted(((v, k) for k, v in sc.items()), reverse=True)
    top, top_key = ranked[0] if ranked else (0.0, "")
    score = 15.0 + top + min(15.0, 0.3 * sum(v for v, _ in ranked[1:]))
    strong = {k for k, v in sc.items() if v >= 0.9 * CATEGORIES[k]["weight"]}
    if {"earnings", "guidance_up"} <= strong or {"earnings", "guidance_down"} <= strong:
        score += 6
    if subj:
        score += 8 if len(subj) <= 3 else 4
        if any(t in MEGA_CAPS for t in subj):
            score += 5
        if watch and any(t in watch for t in subj):
            score += 10
    else:
        if tickers:
            score += 2   # tickers only from the summary / source tags
        if top_key in _MARKET_WIDE and top >= 1.15 * CATEGORIES[top_key]["weight"]:
            score += 8   # market-wide catalyst (Fed decision, CPI print, tariff escalation)
    has_catalyst = big_move or any(k in _CATALYSTS and v >= 0.8 * CATEGORIES[k]["weight"] for k, v in sc.items())
    if stype == "press":
        score += 6 if has_catalyst else 0
    elif stype == "filing":
        score += 5 if has_catalyst else -6
    elif stype == "official":
        score += 8
    elif stype == "social":
        score -= 14
    score += 14.0 * min(1.0, abs(s))
    if _DEAL.search(title) and any(k in sc for k in ("mna", "contract", "offering", "activist")):
        score += 8 if "mna" in sc else 4
    if not subj:
        if top_key in _CATALYSTS or top_key == "price_move":
            score *= 0.75  # company news without a US-listed symbol (foreign / private company)
        elif top_key in _MARKET_WIDE and _FOREIGN.search(title) and not _US.search(title):
            score *= 0.65  # non-US macro (UK CPI, RBI, ...)
    if flags.get("opinion"):
        score = min(score * 0.45, 30.0)
    else:
        if flags.get("analysis"):
            score *= 0.75
        if _OPINION_PUBLISHERS.search(str(extra.get("publisher") or item.get("author") or "")):
            score *= 0.85
    if flags.get("spam"):
        score = min(score * 0.4, 25.0)
    if stype == "social":
        score = min(score, 50.0)
    if flags.get("junk"):
        score = min(score, 3.0)
    return round(max(0.0, min(100.0, score)), 1)


def analyze(item: dict, cfg: dict | None = None, text_title: str | None = None) -> dict:
    """Categories + sentiment + importance for one news dict (does not mutate it). Also returns the per-text
    category factors (`title_cats`, `summary_cats`)."""
    title = text_title if text_title is not None else (item.get("title") or "")
    summary = item.get("summary") or ""
    tc, t_raw = _features(title, True)
    sc, s_raw = _features(summary)
    scores = _combine_scores(tc, sc, title)
    s = _combine_sentiment(t_raw, s_raw, item.get("extra"), bool(_RX_TITLE_MOVE.search(title)))
    flags = _flags(title, summary)
    if flags["spam"] and "lawsuit" not in scores:
        scores["lawsuit"] = CATEGORIES["lawsuit"]["weight"] * 0.5
    cats = sorted(scores, key=lambda k: -scores[k])
    if flags["opinion"]:
        cats.append("opinion")
    tmp = dict(item, title=title, sentiment=s)
    return {"categories": cats, "sentiment": s, "sentiment_label": sentiment_label(s),
            "importance": importance(tmp, cfg, scores, flags), "scores": scores, "flags": flags,
            "title_cats": tc, "summary_cats": sc}


__all__ = ["CATEGORIES", "SENTIMENTS", "classify", "category_scores", "sentiment", "sentiment_label", "importance",
           "analyze", "is_opinion", "is_lawfirm_spam", "is_junk_title", "is_roundup", "outcomes", "features",
           "MEGA_CAPS"]
