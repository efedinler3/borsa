"""US symbol universe (nasdaqtrader.com) and ticker extraction from headlines / summaries."""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request

from ..util import DATA_DIR, http_get, jdump, now_ts

log = logging.getLogger("marketdesk.analysis")

SYMBOLS_PATH = DATA_DIR / "symbols.json"
# HTTPS is behind a bot wall that sometimes serves an HTML challenge; the FTP mirror has the same files.
SYMBOL_URLS = (
    ("nasdaq", "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
     "ftp://ftp.nasdaqtrader.com/symboldirectory/nasdaqlisted.txt"),
    ("other", "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
     "ftp://ftp.nasdaqtrader.com/symboldirectory/otherlisted.txt"),
)
MAX_AGE = 7 * 86400
EXCHANGES = {"Q": "NASDAQ", "A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
MAX_TICKERS = 6

# ---------------------------------------------------------------- curated company names
# "TICKER: alias | alias". Prefix "*" = case-insensitive (distinctive name), "~" = only with company
# context ("Target's", "Target Corp", "Target shares", "Target (TGT)"), "^" = bank/broker/rating agency:
# skipped when the name is only the source of a rating or comment ("Goldman upgrades Nvidia").
# Ticker "_" lists phrases that must never match ("Harrison Ford", "Delta variant").
_COMPANIES = """
AAPL: Apple | Apple Inc
MSFT: *Microsoft
NVDA: *Nvidia
AMZN: Amazon | Amazon.com
GOOGL: Alphabet | Google
META: Meta Platforms | Meta | Facebook
TSLA: *Tesla
AVGO: *Broadcom
AMD: AMD | Advanced Micro Devices
INTC: Intel
NFLX: *Netflix
PLTR: *Palantir
COIN: *Coinbase
MSTR: *MicroStrategy | Strategy Inc
BA: Boeing
JPM: ^JPMorgan | ^JPMorgan Chase | ^JP Morgan | ^J.P. Morgan
GS: ^Goldman Sachs | ^Goldman
MS: ^Morgan Stanley
BAC: ^Bank of America | ^BofA
C: ^Citigroup | ^Citi
WFC: ^Wells Fargo
BLK: BlackRock
BRK.B: Berkshire Hathaway | Berkshire
QCOM: *Qualcomm
ARM: Arm Holdings | ~Arm
TSM: TSMC | Taiwan Semiconductor
SMCI: Super Micro Computer | Super Micro | Supermicro
SOFI: SoFi | SoFi Technologies
HOOD: *Robinhood
SNOW: Snowflake
CRM: *Salesforce
ORCL: Oracle
IBM: IBM
MU: Micron
XOM: Exxon | ExxonMobil | Exxon Mobil
CVX: Chevron
PFE: *Pfizer
MRNA: *Moderna
LLY: Eli Lilly | ~Lilly
NVO: Novo Nordisk | ~Novo
UNH: *UnitedHealth | UnitedHealthcare
WMT: *Walmart
COST: Costco
TGT: ~Target
DIS: Disney | Walt Disney
NKE: Nike
SBUX: *Starbucks
MCD: McDonald's
F: Ford Motor | ~Ford
GM: General Motors | GM
RIVN: *Rivian
LCID: Lucid Group | Lucid Motors | ~Lucid
UBER: Uber
ABNB: *Airbnb
ADBE: Adobe
AMAT: Applied Materials
LRCX: Lam Research
KLAC: KLA Corp | ~KLA
ASML: ASML
MRVL: Marvell | Marvell Technology
TXN: Texas Instruments
ADI: Analog Devices
ON: onsemi | ON Semiconductor
NXPI: NXP Semiconductors | NXP
MCHP: Microchip Technology
GFS: GlobalFoundries
WDC: Western Digital
STX: Seagate
SNDK: Sandisk | SanDisk
DELL: Dell Technologies | ~Dell
HPQ: HP Inc
HPE: Hewlett Packard Enterprise | HPE
CSCO: Cisco
ANET: Arista Networks
PANW: Palo Alto Networks
CRWD: *CrowdStrike
FTNT: Fortinet
ZS: Zscaler
NET: Cloudflare
DDOG: Datadog
NOW: ServiceNow
WDAY: Workday
INTU: Intuit
SHOP: Shopify
PYPL: *PayPal
XYZ: Block Inc | ~Block
SPOT: Spotify
MDB: MongoDB
TEAM: Atlassian
HUBS: HubSpot
TWLO: Twilio
ZM: Zoom Video | Zoom Communications
DUOL: Duolingo
APP: AppLovin
RBLX: Roblox
U: Unity Software
PINS: Pinterest
SNAP: Snap Inc | Snapchat | ~Snap
RDDT: Reddit Inc | Reddit shares | Reddit stock
LYFT: Lyft
DASH: DoorDash
EBAY: eBay
ETSY: Etsy
CHWY: Chewy
W: Wayfair
CVNA: Carvana
OPEN: Opendoor
AFRM: Affirm Holdings | ~Affirm
UPST: Upstart
HIMS: Hims & Hers
PTON: Peloton
GME: *GameStop
AMC: AMC Entertainment
AMCX: AMC Networks
ROKU: Roku
NIO: NIO | Nio
XPEV: XPeng | Xpeng
LI: Li Auto
BABA: *Alibaba
JD: JD.com
BIDU: Baidu
PDD: PDD Holdings | Pinduoduo | Temu
MARA: MARA Holdings | Marathon Digital
RIOT: Riot Platforms
CLSK: CleanSpark
IONQ: IonQ
RGTI: Rigetti
QBTS: D-Wave
SOUN: SoundHound
AI: C3.ai
BBAI: BigBear.ai
RKLB: Rocket Lab
ASTS: AST SpaceMobile
JOBY: Joby Aviation
ACHR: Archer Aviation
CRWV: *CoreWeave
NBIS: Nebius
CRCL: Circle Internet
ALAB: Astera Labs
CRDO: Credo Technology
VRT: Vertiv
OKLO: Oklo
SMR: NuScale
CEG: Constellation Energy
VST: Vistra
NEE: NextEra
FSLR: First Solar
ENPH: Enphase
PLUG: Plug Power
QS: QuantumScape
ALB: Albemarle
FCX: Freeport-McMoRan
NEM: Newmont
NUE: Nucor
CLF: Cleveland-Cliffs
AA: Alcoa
DOW: Dow Inc
DD: DuPont
LMT: Lockheed Martin | Lockheed
RTX: RTX | Raytheon
NOC: Northrop Grumman | Northrop
GD: General Dynamics
LHX: L3Harris
CAT: Caterpillar
DE: Deere | John Deere
HON: Honeywell
GE: GE Aerospace | General Electric
GEV: GE Vernova
GEHC: GE HealthCare
MMM: ~3M
UPS: UPS | United Parcel Service
FDX: FedEx
UNP: Union Pacific
HD: Home Depot
LOW: Lowe's
BBY: Best Buy
TJX: TJX
ROST: Ross Stores
M: Macy's
KSS: Kohl's
GAP: Gap Inc
ULTA: Ulta Beauty
EL: Estee Lauder | Estée Lauder
LULU: Lululemon
DG: Dollar General
DLTR: Dollar Tree
KR: Kroger
KO: Coca-Cola
PEP: PepsiCo | Pepsi
PG: Procter & Gamble | P&G
JNJ: Johnson & Johnson | J&J
MRK: Merck
ABBV: AbbVie
ABT: Abbott Laboratories | Abbott
AMGN: Amgen
GILD: Gilead
REGN: Regeneron
VRTX: Vertex Pharmaceuticals
BIIB: Biogen
BMY: Bristol Myers Squibb | Bristol-Myers Squibb | Bristol Myers
AZN: AstraZeneca
BNTX: BioNTech
NVAX: Novavax
VKTX: Viking Therapeutics
CVS: CVS Health | CVS
HUM: Humana
CI: Cigna
ELV: Elevance
ISRG: Intuitive Surgical
TMO: Thermo Fisher
DHR: Danaher
MDT: Medtronic
SYK: Stryker
BSX: Boston Scientific
DXCM: Dexcom
V: ~Visa
MA: Mastercard
AXP: American Express | Amex | AmEx
SCHW: Charles Schwab | ~Schwab
COF: Capital One
SPGI: ^S&P Global
MCO: ^Moody's
CME: CME Group
ICE: Intercontinental Exchange
NDAQ: Nasdaq Inc
VZ: Verizon
T: AT&T
TMUS: T-Mobile
CMCSA: Comcast
SKYD: Paramount Skydance | ~Paramount
WMG: Warner Music Group
FOXA: Fox Corp
COP: ConocoPhillips
OXY: Occidental Petroleum | Occidental
SLB: SLB | Schlumberger
HAL: Halliburton
MPC: Marathon Petroleum
VLO: Valero
PSX: Phillips 66
KMI: Kinder Morgan
LNG: Cheniere
DVN: Devon Energy
EOG: EOG Resources
FANG: Diamondback Energy
CCL: Carnival
RCL: Royal Caribbean
NCLH: Norwegian Cruise
DAL: Delta Air Lines | Delta Airlines
UAL: United Airlines
AAL: American Airlines
LUV: Southwest Airlines
BKNG: Booking Holdings
EXPE: Expedia
MAR: Marriott
HLT: Hilton
LVS: Las Vegas Sands
WYNN: Wynn Resorts
MGM: MGM Resorts
DKNG: DraftKings
CMG: Chipotle
YUM: Yum Brands | Yum! Brands
PM: Philip Morris
MO: Altria
KHC: Kraft Heinz
MDLZ: Mondelez
HSY: Hershey
GIS: General Mills
TSN: Tyson Foods
ADM: Archer-Daniels-Midland
CL: Colgate-Palmolive
KMB: Kimberly-Clark
CELH: Celsius Holdings
MNST: Monster Beverage
ACN: Accenture
TTWO: Take-Two
SIRI: Sirius XM
DJT: Trump Media
GLW: Corning
APH: Amphenol
ETN: Eaton
AZO: AutoZone
TM: Toyota
HMC: Honda
SONY: Sony
STLA: Stellantis
RACE: Ferrari
NVS: Novartis
SNY: Sanofi
GSK: GSK
BP: BP
SHEL: Shell plc | ~Shell
UL: Unilever
HSBC: ^HSBC
UBS: ^UBS
DB: ^Deutsche Bank
BCS: ^Barclays
JEF: ^Jefferies
NOK: Nokia
ERIC: Ericsson
SAP: SAP
MELI: MercadoLibre
NU: Nu Holdings | Nubank
SE: Sea Ltd | Sea Limited
GRAB: Grab Holdings
CPNG: Coupang
TEVA: Teva
INFY: Infosys
BE: Bloom Energy
FCEL: FuelCell Energy
RNG: RingCentral
DGX: Quest Diagnostics
ALLE: Allegion
RHI: Robert Half
FICO: FICO | Fair Isaac
FIX: Comfort Systems
TLRY: Tilray
DECK: Deckers Outdoor | Deckers Brands
AARD: Aardvark Therapeutics
COCO: Vita Coco
REXR: Rexford Industrial
MXL: MaxLinear
APLD: Applied Digital
DVA: DaVita
VEEV: Veeva Systems | ~Veeva
EWBC: East West Bancorp
GPC: Genuine Parts
AEM: Agnico Eagle
POR: Portland General Electric
ELME: Elme Communities
BHVN: Biohaven
SOLV: Solventum
BAX: Baxter International | ~Baxter
EXEL: Exelixis
SYK: Stryker
KN: Knowles Corp
TSCO: Tractor Supply
NVR: NVR Inc
WH: Wyndham Hotels
KALU: Kaiser Aluminum
PCG: PG&E
CRDO: ~Credo
_: Harrison Ford | Gerald Ford | Ford Foundation | Henry Ford Health | Big Apple | Delta variant | Target date | Target price | Target Field | Visa Waiver | Apple Valley | Shell Shock | Meta Quest Pro
B: Barrick Mining | Barrick Gold | ~Barrick
FIG: Figma
KLAR: Klarna
CHYM: Chime Financial
ETOR: eToro
GLXY: Galaxy Digital
IREN: IREN
CIFR: Cipher Mining | Cipher Digital
HUT: Hut 8
BMNR: BitMine | Bitmine
LITE: Lumentum
HTZ: Hertz
KVUE: Kenvue
LUMN: Lumen Technologies
CORZ: Core Scientific
TEM: Tempus AI
SYM: Symbotic
CAVA: Cava Group
SPCX: *SpaceX | Space Exploration Technologies
SKHY: *SK Hynix
ACA: Arcosa
CART: Instacart | Maplebear
BX: Blackstone
SPY: SPDR S&P 500 ETF
QQQ: Invesco QQQ
IWM: iShares Russell 2000
ARKK: ARK Innovation ETF
GLD: SPDR Gold
IBIT: iShares Bitcoin Trust
"""

# Popular ETFs allowed as bare uppercase tokens (other ETFs need a cashtag / explicit pattern / watchlist).
POPULAR_ETFS = set("""SPY QQQ IWM DIA VOO VTI TLT HYG LQD GLD SLV USO UNG XLE XLF XLK XLV XLY XLP XLI XLU XLB
XLRE XLC SMH SOXX SOXL SOXS TQQQ SQQQ SPXL SPXS UVXY VXX ARKK IBIT FBTC ETHA KRE KWEB FXI EEM EFA GDX GDXJ
TNA TZA BITO""".split())

# Uppercase tokens that are tickers on some exchange but mostly mean something else in news text.
BLACKLIST = set("""
CEO CFO COO CTO CIO CMO CRO EVP SVP CHAIR FDA SEC FTC DOJ DOE DOD DOT DHS FAA FCC FDIC FHFA FINRA CFTC OCC CFPB EPA
IRS FBI CIA NSA NIH CDC CMS HHS USDA NASA NATO OPEC IMF WTO WHO ECB BOJ BOE BOC RBA RBI SNB PBOC FOMC FED GDP GNP
CPI PPI PCE PMI ISM ADP NFP JOLTS USD EUR GBP JPY CNY CAD AUD CHF INR KRW HKD BTC ETH XRP SOL DOGE USDC USDT ETF
ETFS ETN ETP IPO IPOS SPAC SPACS REIT REITS ADR ADRS ADS ESG EPS EBIT EBITDA YOY QOQ MOM TTM ARR ARPU AUM NAV ROI
ROE ROA ROIC CAGR FCF DCF NPV IRR LBO MBO GAAP IFRS EV EVS SUV SUVS AI AGI GPU GPUS CPU CPUS TPU NPU HBM DRAM
NAND SSD LLM LLMS API APIS SAAS PAAS IOT AR VR XR UAV OEM ODM RFP NDA LOI MOU USA UK EU UAE US PRC NYC LA DC SF
NBA NFL NHL MLB MLS NCAA PGA UFC WWE FIFA ESPN HBO CNN CNBC BBC NPR PBS ABC CBS NBC FOX MSNBC WSJ NYT AP AFP UPI
IBD MSN AOL TSX LSE HKEX SSE ASX NSE BSE KRX TSE JPX DAX CAC FTSE IBEX KOSPI HSI SPX NDX DJIA DJI RUT VIX VVIX WTI
LNG LPG EST EDT PST PDT CST CDT GMT UTC ET PT CT AM PM FY FYE LTM YTD MTD ATH ATL IMO TBD TBA FAQ PDF URL FOMO
YOLO HODL WSB DD TLDR LOL OMG WTF FUD ATM OTC NYSE AMEX NASDAQ CBOE LLC INC CORP LTD PLC LLP SA AG NV GMBH II III
IV VI VII VIII IX XI XII JR SR MR MRS DR PHD MBA CPA CFA ESOP IRA HSA FSA HMO PPO ACA ICU ER COVID SARS HIV AIDS
DNA RNA PDUFA BLA SNDA IND EUA CRL ODD RMAT ANDA ASCO ESMO AACR CES MWC GTC WWDC MAGA GOP DNC RNC POTUS SCOTUS
USMCA NAFTA CHIPS CARES PPP SBA HUD FHA VA TSA ICE CBP DEA ATF SSA USPS DMV NHTSA NTSB OSHA NLRB EEOC FERC NRC
ERCOT PJM EIA IEA HY IG CDS CDO CLO MBS ABS CMBS RMBS TIPS SOFR LIBOR RRP QE QT ZIRP BRICS ASEAN UN EVP ADHD
THE AND FOR ARE BUT NOT YOU ALL ANY CAN HAD HAS HER WAS ONE OUR OUT DAY GET HIM HIS HOW MAN NEW NOW OLD SEE TWO
WAY BOY DID ITS LET PUT SAY SHE TOO USE BIG TOP LOW HIGH WIN WINS WON LOSS GAIN GAINS LOSE BUY SELL HOLD CALL
CALLS PUTS LONG SHORT BULL BEAR BULLS BEARS MOON PUMP DUMP RUN RIP FLY JUMP SOAR DROP FALL RISE CASH LOVE HOPE
FAST REAL OPEN BEST GOOD WELL EVER LIFE MAIN SAFE TRUE KEY JOB JOBS CAR CARS BOX EAT FUN PLAY PLUS HUGE EARN GROW
ACT AGE AIR ART BAND BEAT BLUE BOOM CARE CORE DEAL EDGE ELSE EVEN FARM FEED FIVE FLOW FOOD FORM FREE FROM FUEL
FULL FUND GAME GOLD GRID HALF HEAR HEAT HELP HERE HERO HOME HOST HOUR IDEA INFO IRON JUST KIDS KIND KING LAND
LAST LEAD LIKE LINE LIVE LOAN LOCK LUCK MARK MASS MEET MIND MISS MORE MOST MOVE MUCH MUST NEAR NEED NEXT NICE
NINE NOTE ONCE ONLY OVER PACK PAGE PAID PARK PART PASS PAST PATH PEAK PICK PLAN POOL POST PURE RARE RENT REST
RICH RIDE RING RISK ROAD ROCK ROLE ROOM ROOT RULE RUSH SALE SAVE SEAT SEED SELF SEND SHIP SHOW SIGN SITE SIZE
SKIN SLOW SOFT SOLD SOME SONG SOON SORT STAR STAY STEP STOP SUCH SUIT SURE TAKE TALK TALL TANK TELL TERM TEST
TEXT THAN THAT THEM THEN THEY THIS TIME TINY TOOK TOOL TOUR TOWN TREE TRIP TURN TYPE UNIT UPON USED USER VAST
VERY VIEW VOTE WAIT WALK WALL WANT WARM WAVE WEAR WEEK WENT WERE WEST WHAT WHEN WIDE WILD WILL WIND WINE WING
WIRE WISE WISH WITH WOOD WORD WORK YARD YEAR YOUR ZERO ZONE TECH DATA RATE RATES CUT CUTS HIKE BILL BOND
NEWS ALERT UPDATE BREAKING LIVE WATCH STOCK STOCKS SHARE SHARES PRICE MARKET TRADE TRADER WEEKLY DAILY TODAY
SAN LAS LOS SANTA PALO NORTH SOUTH EAST CITY FORT PORT LAKE BAY BEACH HILL HILLS PARK GLEN GIC AEC PC
""".split())

_CTX_AFTER = (r"(?=(?:'s\b)|\s+(?:Inc\b|Corp\b|Corporation\b|Co\b|Holdings\b|Group\b|plc\b|shares\b|stock\b|"
              r"earnings\b|CEO\b|results\b|revenue\b|sales\b|quarter\b|said\b|says\b)|\s*\((?:[A-Za-z ]+:\s*)?[A-Z]{1,5}\))")
_BROKER_AFTER = re.compile(
    r"\s*(?:'s\s+)?(?:\w+\s+)?(?:analysts?|analyst|strategists?|economists?|research|upgrades?|upgraded|downgrades?|"
    r"downgraded|initiates?|reiterates?|maintains?|says|said|sees|expects|predicts|forecasts?|warns|rates|note|"
    r"survey|estimates?)\b|\s*(?:raises|lifts|boosts|cuts|lowers|trims|hikes|ups)\b[^.;:]{0,50}\btarget",
    re.I)
_BROKER_BEFORE = re.compile(r"(?:\bat|\bfrom|\bby|\baccording to|\bper|\bvia|\banalysts?|\bstrategists?|\beconomists?)\s+$", re.I)

_RIC_SUFFIX = re.compile(r"\.(?:O|N|OQ|K|A|P|Z)$")


def _broker_context(text: str, start: int, end: int) -> bool:
    """True when a bank/broker name is only the source of a rating or comment."""
    return bool(_BROKER_AFTER.match(text, end) or _BROKER_BEFORE.search(text, max(0, start - 24), start))


_lock = threading.Lock()
_syms: dict[str, tuple[str, bool]] | None = None
_bg_started = False
_alias_ticker: dict[str, str] = {}
_alias_broker: set[str] = set()
_broker_syms: set[str] = set()
_names_cs: dict[str, list[re.Pattern]] | None = None
_names_ci: dict[str, list[re.Pattern]] = {}
_curated_tickers: set[str] = set()
_curated_names: dict[str, str] = {}
_sym_rx: dict[str, list[re.Pattern]] = {}   # curated symbol -> its alias patterns (for mention_span)


def normalize_symbol(sym: str) -> str:
    """'$brk-b' / 'BRK/B' -> 'BRK.B'. Returns '' for obviously invalid input."""
    s = (sym or "").strip().upper().lstrip("$")
    s = s.replace("/", ".").replace("-", ".")
    return s if re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z]{1,2})?", s) else ""


# ---------------------------------------------------------------- universe

def _clean_name(name: str) -> str:
    n = name.strip()
    n = re.sub(r"\s+-\s+.*$", "", n)
    n = re.sub(r"\s+(?:New\s+)?(?:Class [A-Z] )?(?:Common Stock|Ordinary Shares|Common Shares|Capital Stock|"
               r"Subordinate Voting Shares|American Depositary Shares|Depositary Shares|ADS|Shares of Beneficial Interest)"
               r"\b.*$", "", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip(" ,")


_SKIP_NAME = re.compile(r"\b(?:Warrants?|Units?|Rights?|Preferred|Notes due|Senior Notes|Subordinated|Debentures?|"
                        r"% Notes|Test Stk|Test Issue)\b", re.I)


def _parse(kind: str, text: str) -> dict[str, list]:
    out: dict[str, list] = {}
    lines = text.splitlines()
    if not lines:
        return out
    header = [h.strip() for h in lines[0].split("|")]
    idx = {h: i for i, h in enumerate(header)}
    sym_col = "Symbol" if kind == "nasdaq" else "ACT Symbol"
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        p = line.split("|")
        try:
            sym = p[idx[sym_col]].strip()
            name = p[idx["Security Name"]].strip()
            if p[idx["Test Issue"]].strip() == "Y":
                continue
            etf = p[idx["ETF"]].strip() == "Y"
            exch = "Q" if kind == "nasdaq" else p[idx["Exchange"]].strip()
        except (KeyError, IndexError):
            continue
        if not sym or "$" in sym or re.search(r"\.(?:W|WS|U|R|RT)$", sym) or _SKIP_NAME.search(name):
            continue
        if not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z]{1,2})?", sym):
            continue
        out[sym] = [_clean_name(name) or sym, 1 if etf else 0, EXCHANGES.get(exch, exch)]
    return out


def _download(url: str, ftp_url: str) -> str:
    try:
        text = http_get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20, retries=0).text
        if "|Security Name|" in text[:200]:
            return text
    except Exception as e:  # noqa: BLE001
        log.debug("nasdaqtrader https failed: %s", e)
    with urllib.request.urlopen(ftp_url, timeout=20) as r:
        text = r.read().decode("utf-8", "replace")
    if "|Security Name|" not in text[:200]:
        raise RuntimeError("unexpected symbol file format")
    return text


def refresh_symbols(cfg: dict | None = None, force: bool = False) -> int:
    """Download the nasdaqtrader symbol directory into DATA_DIR/symbols.json (if older than 7 days or forced).
    Returns the number of symbols available."""
    global _syms
    if not force and SYMBOLS_PATH.exists():
        try:
            data = json.loads(SYMBOLS_PATH.read_text(encoding="utf-8"))
            if now_ts() - int(data.get("ts") or 0) < MAX_AGE and len(data.get("symbols") or {}) > 3000:
                if _syms is None:
                    _set_universe(data["symbols"])
                return len(data["symbols"])
        except (OSError, ValueError, TypeError):
            pass
    symbols: dict[str, list] = {}
    errors = []
    for kind, url, ftp_url in SYMBOL_URLS:
        try:
            symbols.update(_parse(kind, _download(url, ftp_url)))
        except Exception as e:  # noqa: BLE001 - one file failing should not lose the other
            errors.append(f"{kind}: {e}")
    if len(symbols) < 3000:
        if SYMBOLS_PATH.exists() and _syms:
            log.warning("symbol refresh failed (%s); keeping cached universe", "; ".join(errors))
            return len(_syms)
        raise RuntimeError("symbol universe download failed: " + ("; ".join(errors) or "too few symbols"))
    tmp = SYMBOLS_PATH.with_suffix(".tmp")
    tmp.write_text(jdump({"ts": now_ts(), "count": len(symbols), "symbols": symbols}), encoding="utf-8")
    tmp.replace(SYMBOLS_PATH)
    _set_universe(symbols)
    log.info("symbol universe refreshed: %d symbols", len(symbols))
    return len(symbols)


def _set_universe(symbols: dict) -> None:
    global _syms, _gen_index
    _syms = {s: (v[0], bool(v[1])) for s, v in symbols.items() if isinstance(v, list) and len(v) >= 2}
    _gen_index = None


def _bg_refresh() -> None:
    try:
        refresh_symbols()
    except Exception as e:  # noqa: BLE001
        log.warning("background symbol refresh failed: %s", e)


def universe() -> dict[str, tuple[str, bool]]:
    """{symbol: (name, is_etf)}; empty dict until symbols.json exists (a background download is started)."""
    global _syms, _bg_started
    if _syms is None:
        with _lock:
            if _syms is None:
                stale = True
                try:
                    data = json.loads(SYMBOLS_PATH.read_text(encoding="utf-8"))
                    _set_universe(data.get("symbols") or {})
                    stale = now_ts() - int(data.get("ts") or 0) > MAX_AGE
                except (OSError, ValueError, TypeError, AttributeError):
                    _syms = {}
                if (not _syms or stale) and not _bg_started:
                    _bg_started = True
                    threading.Thread(target=_bg_refresh, name="symbols-refresh", daemon=True).start()
    return _syms or {}


def is_known(sym: str) -> bool:
    u = universe()
    return sym in u if u else sym in _curated_tickers


def is_etf(sym: str) -> bool:
    v = universe().get(sym)
    return bool(v and v[1]) or sym in POPULAR_ETFS


def company_name(sym: str) -> str | None:
    s = normalize_symbol(sym)
    if not s:
        return None
    v = universe().get(s)
    if v:
        return v[0]
    _compile()
    return _curated_names.get(s)


# ---------------------------------------------------------------- extraction

_APOS = str.maketrans({"\u2019": "'", "\u2018": "'", "\u00a0": " "})
_RX_CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)(?![\w])")
_RX_EXCH = re.compile(
    r"(?i:\b(?:nasdaq(?:gs|gm|cm)?(?: global(?: select)?(?: market)?| capital market| stock market)?|"
    r"nyse(?: american| arca| mkt)?|nyseamerican|amex|cboe(?: bzx)?|otc(?:qx|qb|bb|mkts| markets| pink)?))"
    r"\s*[:\uff1a]\s*\$?([A-Z]{1,5}(?:[.\-][A-Z]{1,2})?)\b")
_RX_LISTED = re.compile(r"\b(?:Nasdaq|NASDAQ|NYSE)-listed\s+([A-Z]{2,5})\b")
_RX_SYMBOL_PHRASE = re.compile(
    r"(?i:\b(?:ticker(?: symbol)?|trading symbol|stock symbol|under the (?:ticker|symbol)(?: symbol)?))"
    r"\s*[:\"'(]?\s*\"?\$?([A-Z]{1,5})\b")
_RX_TICKER_EXCH = re.compile(r"\(\$?([A-Z]{1,5}(?:\.[A-Z])?):(?i:NYSE|NASDAQ|NYSEARCA|NYSEAMERICAN|BATS|CBOE|OTCMKTS|OTC|OTCQX|OTCQB)\)")
_RX_PARENS = re.compile(r"([A-Za-z][\w.&'\-]*)\s+\(\$?([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\)")
_RX_BARE = re.compile(r"(?<![\w$.\-/@&])([A-Z]{2,5}(?:\.[A-Z])?)(?![\w\-/&]|\.[A-Za-z]|'[A-Za-z]{2})")
_RX_TOKEN = re.compile(r"(?<![\w$./@])[A-Za-z0-9](?:[A-Za-z0-9&]|\.(?=[A-Za-z0-9]))*")


def _first_key(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9&.]", "", re.split(r"[\s'\-]", s, 1)[0]).rstrip(".")


def _compile() -> None:
    global _names_cs
    if _names_cs is not None:
        return
    with _lock:
        if _names_cs is not None:
            return
        cs: dict[str, list] = {}
        ci: dict[str, list] = {}
        for line in _COMPANIES.strip().splitlines():
            if ":" not in line:
                continue
            sym, aliases = line.split(":", 1)
            sym = sym.strip()
            if sym != "_":
                _curated_tickers.add(sym)
            first = True
            for raw in aliases.split("|"):
                a = raw.strip()
                if not a:
                    continue
                flags = ""
                while a and a[0] in "*~^":
                    flags += a[0]
                    a = a[1:]
                if first:
                    _curated_names[sym] = a
                    first = False
                key = a.lower()
                _alias_ticker[key] = sym
                if "^" in flags:
                    _alias_broker.add(key)
                    _broker_syms.add(sym)
                frag = re.escape(a).replace(r"\ ", r"\s+")
                if "~" in flags:
                    frag += _CTX_AFTER
                # A hyphen followed by a capital joins two names ("Tesla-SpaceX", "Apple-Google"); "Meta-owned" does not.
                rx = re.compile(r"(" + frag + r")(?!\w|-(?!(?-i:[A-Z]))|\.[A-Za-z])", re.I if "*" in flags else 0)
                _sym_rx.setdefault(sym, []).append(rx)
                if "*" in flags:
                    ci.setdefault(_first_key(a).lower(), []).append((len(a), rx))
                else:
                    cs.setdefault(_first_key(a), []).append((len(a), rx))
        for d in (cs, ci):
            for k in d:
                d[k] = [rx for _, rx in sorted(d[k], key=lambda x: -x[0])]
        _names_ci.update(ci)
        _names_cs = cs


# ---------------------------------------------------------------- generic company names from the universe
_LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "nv", "n.v", "sa",
          "s.a", "ag", "se", "llc", "lp", "l.p", "sponsored", "adr", "ads", "the", "class", "a", "b", "c", "new"}
# A key ending in one of these needs no extra context ("Bloom Energy", "Aardvark Therapeutics").
_INDUSTRY = {"Therapeutics", "Pharmaceuticals", "Pharma", "Biosciences", "Bioscience", "Biotherapeutics", "Biopharma",
             "Biopharmaceuticals", "Biologics", "Oncology", "Genomics", "Diagnostics", "Bancorp", "Bancshares",
             "Financial", "Technologies", "Semiconductor", "Semiconductors", "Robotics", "Airlines", "Resorts",
             "Realty", "Brands", "Holdings", "Group", "Industries", "Systems", "Energy", "Entertainment", "Networks",
             "Software", "Motors", "Minerals", "Mining", "Petroleum", "Communications", "Interactive", "Aerospace",
             "Biotech", "Automotive", "Healthcare", "Biomedical", "Medical", "Pharmaceutical", "Laboratories",
             "Bank", "Banc", "Insurance", "REIT", "Properties", "Hospitality"}
_CONTEXT_NEXT = {"inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "holdings", "group", "ltd", "ltd.", "plc",
                 "stock", "stocks", "shares", "shareholders", "sa", "nv", "n.v.", "ag"}
_RX_WORD = re.compile(r"[A-Za-z0-9&]+(?:[.'\-][A-Za-z0-9&]+)*\.?")
_gen_index: dict[tuple[str, str], list[tuple[tuple[str, ...], str, bool]]] | None = None
_gen_first: set[str] = set()
_gen_lock = threading.Lock()
# One-word universe names ("Autoliv", "Gentherm", "Braze") match only right before stock / shares / a price verb.
_one_tok: dict[str, str] = {}
_ONE_TOK_STOP = set("""
Eastern Southern Citizens Universal Popular People Quantum Bullish Solana Spire Nasdaq Reddit Noble Orion Merlin
Meridian Vertex Vantage Reliance Compass Agora Aware Banner Happen Interface Fluent Dover Strategy Target Block Shell
Graham Hawkins Lindsay Pearson Kirby Marcus Rogers Regis Rollins Brady Sinclair Ennis Marsh Lineage Largo Cadiz Stewards
Momentus Genesis Unity Insight Pioneer Frontier Summit Liberty Patriot Heritage Legacy Premier Select Victory Fusion
""".split())
_RX_ONE_TOK = re.compile(
    r"(?<![\w$.\-&'])([A-Z][A-Za-z]{4,})(?=(?:'s)?\s+(?i:stock|shares|surges?|surged|soars?|soared|jumps?|jumped|"
    r"falls?|fell|slides?|slid|plunges?|plunged|tumbles?|tumbled|sinks?|sank|rall(?:y|ies|ied)|rises|rose|climbs?|"
    r"climbed|drops?|dropped|spikes?|spiked|skyrockets?|skyrocketed|craters?|cratered|slumps?|slumped|"
    r"company|inc|corp|corporation|holdings|plc|ltd)\b|"
    r"\s*\((?:(?:NYSE|NASDAQ|Nasdaq)\s*:\s*)?[A-Z]{1,5}\))")


def _name_tokens(name: str) -> list[str]:
    toks = [t.rstrip(".") for t in _RX_WORD.findall(name.replace(",", " "))]
    while toks and toks[-1].lower() in _LEGAL:
        toks.pop()
    return toks


def _index_name(name: str) -> str:
    """Universe name as headlines write it: no '(The)' and no share-class tail ("Gildan Activewear, Inc. Class A
    Sub. Vot." -> "Gildan Activewear, Inc.", "Chemours Company (The)" -> "Chemours Company")."""
    n = re.sub(r"\s*\(The\)", "", name, flags=re.I)
    return re.split(r",?\s+(?:Class\s+[A-Z]\b|Sub\.?\s*Vot|Series\s+[A-Z]\b|Ordinary\b|Common\b|Non-?Voting\b)", n,
                    maxsplit=1)[0]


def _name_index() -> dict[tuple[str, str], list[tuple[tuple[str, ...], str, bool]]]:
    """(first, second token) -> [(name tokens, symbol, strong)] from universe names (non-ETF, >= 2 tokens)."""
    global _gen_index
    if _gen_index is not None:
        return _gen_index
    u = universe()
    with _gen_lock:
        if _gen_index is not None:
            return _gen_index
        return _build_name_index(u)


def _build_name_index(u: dict) -> dict[tuple[str, str], list[tuple[tuple[str, ...], str, bool]]]:
    global _gen_index
    _compile()
    by_key: dict[tuple[str, ...], list[str]] = {}
    one: dict[str, list[str]] = {}
    for sym, (name, etf) in u.items():
        if etf:
            continue
        toks = _name_tokens(_index_name(name))
        if (len(toks) == 1 and len(toks[0]) >= 5 and toks[0].isalpha() and toks[0][0].isupper()
                and any(c.islower() for c in toks[0]) and toks[0] not in _ONE_TOK_STOP
                and toks[0].lower() not in _alias_ticker):
            one.setdefault(toks[0], []).append(sym)
        variants = [toks]
        if len(toks) >= 3 and toks[-1] in ("Holdings", "Group", "Inc", "USA", "US", "International", "Worldwide",
                                           "Global", "America", "Americas"):
            variants.append(toks[:-1])
        for v in variants:
            if len(v) >= 2 and not all(t.lower() in _LEGAL for t in v):
                by_key.setdefault(tuple(v), []).append(sym)
    idx: dict[tuple[str, str], list[tuple[tuple[str, ...], str, bool]]] = {}
    for key, syms in by_key.items():
        cur = [x for x in syms if x in _curated_tickers]
        sym = (cur or sorted(syms, key=lambda x: ("." in x, len(x), x)))[0]
        idx.setdefault((key[0], key[1]), []).append((key, sym, key[-1] in _INDUSTRY))
    for lst in idx.values():
        lst.sort(key=lambda x: -len(x[0]))
    if u:
        _gen_first.clear()
        _gen_first.update(k[0] for k in idx)
        _one_tok.clear()
        for tok, syms in one.items():
            if any(s in _curated_tickers for s in syms):
                continue  # curated aliases already cover these companies
            _one_tok[tok] = sorted(syms, key=lambda x: ("." in x, len(x), x))[0]
        _gen_index = idx
    return idx


def _poss(w: str) -> str:
    w = w.rstrip(".")
    return w[:-2] if w.endswith("'s") else w


# Rating headlines name the company without a symbol: "Initiates Coverage of Guardant Health at Buy".
_RATING_AFTER = re.compile(r"\s+(?:at|with (?:a|an))\s+(?:buy|hold|sell|outperform|overweight|underweight|neutral|"
                           r"underperform|market perform|equal[- ]weight)\b", re.I)
_RATING_BEFORE = re.compile(r"\b(?:coverage (?:of|on)|initiates?|initiated|upgrades?|upgraded|downgrades?|downgraded|"
                            r"reiterates?|reiterated|rates?|rated)\s+$", re.I)


def _generic_names(text: str, add) -> list[tuple[int, int]]:
    """Universe company names ("Howard Hughes Holdings", "Bloom Energy"); returns matched spans."""
    spans: list[tuple[int, int]] = []
    idx = _name_index()
    if not idx or not _gen_first.intersection(_RX_WORD.findall(text)):
        return spans
    ms = list(_RX_WORD.finditer(text))
    pw = [_poss(m.group(0)) for m in ms]
    n_words = len(ms)
    i = 0
    while i < n_words - 1:
        cands = idx.get((pw[i], pw[i + 1]))
        if not cands or text[ms[i].end():ms[i + 1].start()] != " ":
            i += 1
            continue
        hit = 0
        for toks, sym, strong in cands:
            n = len(toks)
            if i + n > n_words or any(pw[i + k] != toks[k] for k in range(2, n)):
                continue
            if any(text[ms[i + k].end():ms[i + k + 1].start()] != " " for k in range(1, n - 1)):
                continue
            st, last_end = ms[i].start(), ms[i + n - 1].end()
            nxt = ms[i + n].group(0).lower() if i + n < n_words else ""
            if (strong or nxt in _CONTEXT_NEXT or text[last_end:last_end + 2].lstrip().startswith("(")
                    or _RATING_AFTER.match(text, last_end) or _RATING_BEFORE.search(text, max(0, st - 40), st)):
                if not (sym in _broker_syms and _broker_context(text, st, last_end)):
                    add(sym, st, last_end)
                    spans.append((st, last_end))
                hit = n
                break
        i += hit or 1
    return spans


_RX_ALPHA_WORD = re.compile(r"[A-Za-z]{2,}")


def _mostly_upper(text: str) -> bool:
    """All-caps text (shouted headlines, SEC filer names): bare uppercase tokens mean nothing there."""
    words = _RX_ALPHA_WORD.findall(text)
    return len(words) >= 4 and sum(1 for w in words if not w.isupper()) < 0.25 * len(words)


_RX_CAPS_PREV = re.compile(r"([A-Z][A-Z.&']+)[ ]$")
_RX_CAPS_NEXT = re.compile(r"[ ]([A-Z][A-Z.&']+)\b")


def _in_caps_run(text: str, start: int, end: int, known) -> bool:
    """True for 'SAN DIEGO' / 'NEW YORK' style all-caps phrases (datelines, shouted names)."""
    p = _RX_CAPS_PREV.search(text, max(0, start - 16), start)
    if p and p.end() == start and not known(p.group(1)):
        return True
    n = _RX_CAPS_NEXT.match(text, end)
    return bool(n and len(n.group(1)) >= 2 and not known(n.group(1)) and n.group(1).lower() not in _LEGAL_NEXT)


# Bare-token guards: a capitalized org noun / legal suffix right after the token means it is part of another
# company's name ("ICON Capital Group", "AAC Technologies", "RHI Magnesita N.V."), not the listed symbol.
_ORG_NOUNS = set("""Capital Consulting Partners Group Holdings Technologies Technology Acquisition Acquisitions
Pharmaceuticals Pharma Industries Show Bearings Energy Resources Labs Laboratories Systems Solutions International
Bank Bancorp Financial Therapeutics Biosciences Media Entertainment Networks Communications Motors Mining Minerals
Metals Ventures Investments Securities Insurance Realty Properties Foods Brands Services Software Healthcare Medical
Sciences Logistics Shipping Airlines Aviation Robotics Semiconductor Electronics Gaming Studios Records""".split())
_LEGAL_NEXT = {"n.v.", "nv", "plc", "ag", "asa", "inc", "inc.", "llc", "ltd", "ltd.", "s.a.", "sa", "se", "ab", "corp",
               "corp.", "corporation", "co.", "limited", "gmbh", "s.p.a.", "spa", "oyj", "as", "a/s", "bhd", "tbk"}
_SUFFIX_SYMS = {"ASA", "AG", "SA", "SE", "NV", "AB", "AS", "OYJ", "PLC", "SPA", "BHD", "TBK", "LLC", "LTD"}
_RX_NEXT2 = re.compile(r"\s+([A-Za-z][\w.&'/\-]*)(?:\s+([A-Za-z][\w.&'/\-]*))?")
_RX_PREV_CAP = re.compile(r"[A-Z][\w&'.\-]*\s+$")
_RX_NUM_BEFORE = re.compile(r"\d\s*$")
_RX_BARE_CTX = re.compile(r"(?:'s)?\s*(?:[(:]|(?i:stock|shares?|q[1-4]|earnings|results|price|options|calls?|puts?|"
                          r"investors|(?:share|stock)?holders?|announces|announced|reports|reported|declares|declared|"
                          r"posts|posted|prices|priced|completes|completed|files|filed|appoints|names)\b)")
_RX_BARE_CTX_BEFORE = re.compile(r"(?i:\b(?:reasons?(?: to (?:buy|sell|avoid|own|watch|consider))?|shares of|stake in|"
                                 r"vs\.?|versus)\s+|\b(?:nyse|nasdaq|amex|arca|american|texas|cboe)\s*:\s*)$")
_RX_LIST_PREV = re.compile(r"(?<![\w$.])\$?([A-Z]{1,5}(?:\.[A-Z])?)\s*(?:,|&|/|•|\band\b|\bvs\.?|\bversus\b)\s*$")
_RX_LIST_NEXT = re.compile(r"\s*(?:,|&|/|•|\band\b|\bvs\.?|\bversus\b)\s*\$?([A-Z]{1,5}(?:\.[A-Z])?)(?![\w])")
_RX_CAP_WORD = re.compile(r"[A-Z][\w&'.\-]*")
_RX_ANY_WORD = re.compile(r"[\w&'.\-]+")
_RX_STOCK_CTX = re.compile(r"(?:'s)?\s+(?i:stock|shares?|earnings|results|q[1-4])\b")
_RX_CRYPTO_CTX = re.compile(r"\b(?:price prediction|tokens?|coins?|crypto\w*|memecoins?|altcoins?)\b", re.I)
_RX_ART_CTX = re.compile(r"(?:'s)?\s+(?:(?:is|are)\s+(?:a\s+|an\s+)?(?:risky|cheap|buy|sell|overvalued|undervalued|stocks?)|"
                         r"looks|may|could|falls|rises)\b", re.I)
_RX_BL_NEXT = re.compile(r"\s+(?:stocks?|shares?|risky|cheap|(?:is|are)\s+(?:a\s+|an\s+)?(?:risky|cheap|buy|sell|overvalued|"
                         r"undervalued|stocks?))\b", re.I)
_RX_BL_PREV = re.compile(r"(?i:\b(?:reasons?(?: to (?:buy|sell|avoid|own|watch|consider))?|stock)\s+)$")


def _blacklisted_stock_ctx(text: str, start: int, end: int) -> bool:
    """A stoplisted symbol counts only as "Reasons MTD", "Stock MTD", "MTD is risky", "MTD shares"."""
    return bool(_RX_BL_PREV.search(text, max(0, start - 24), start) or _RX_BL_NEXT.match(text, end))


def _name_words(sym: str) -> list[str]:
    v = universe().get(sym)
    names = [v[0]] if v else []
    if sym in _curated_names:
        names.append(_curated_names[sym])
    return [w.lower().rstrip(".") for n in names for w in _RX_WORD.findall(n.replace(",", " "))]


def _shares_prefix(words: list[str], name_words: list[str]) -> bool:
    """True when one of `words` and one name token share a >= 4-char prefix (or are equal short tokens)."""
    for w in words:
        w = w.lower().rstrip(".")
        if w.endswith("'s"):
            w = w[:-2]
        for t in name_words:
            k = min(4, len(w), len(t))
            if k >= 2 and w[:k] == t[:k] and (k >= 4 or w == t):
                return True
    return False


def _bare_ok(text: str, s: str, start: int, end: int, known, need_ctx: bool) -> bool:
    """Evidence checks for a bare uppercase token of a non-watchlist symbol; `need_ctx` (non-curated symbols in
    news text) additionally requires ticker context: the name starts with the symbol, "XYZ stock", or a list."""
    if _RX_NUM_BEFORE.search(text, max(0, start - 3), start):
        return False                                   # "737 MAX", "Phase 3 DLB"
    name = (universe().get(s) or ("",))[0]
    name_low = name.lower()
    toks = _name_tokens(name)
    first_is_sym = bool(toks) and toks[0].lower() == s.lower()
    nxt = _RX_NEXT2.match(text, end)
    if nxt:
        w1 = nxt.group(1).rstrip(",;:")
        w2 = (nxt.group(2) or "").rstrip(",;:")
        if w1 in _ORG_NOUNS and w1.lower() not in name_low:
            return False                               # "ICON Capital Group", "SXT Pharmaceuticals"
        if not first_is_sym and any(w and w.lower() in _LEGAL_NEXT and w.lower().rstrip(".") not in name_low
                                    for w in (w1, w2)):
            return False                               # "RHI Magnesita N.V.", "TEN Holdings Ltd"
    if s in _SUFFIX_SYMS and _RX_PREV_CAP.search(text, max(0, start - 40), start):
        return False                                   # "Otovo ASA"
    if not need_ctx or first_is_sym:
        return True                                    # "CRH PLC", "LKQ", "PJT Partners"
    if _RX_CRYPTO_CTX.search(text):
        # Coin pages: uppercase tokens are coin symbols ("this CAKE price prediction"); only stock wording counts.
        return bool(_RX_STOCK_CTX.match(text, end))
    if (_RX_BARE_CTX.match(text, end) or _RX_ART_CTX.match(text, end)
            or _RX_BARE_CTX_BEFORE.search(text, max(0, start - 40), start)):
        return True                                    # "TREX stock", "GNTX Q3", "Reasons to sell NEOG", "XYZ looks"
    p = _RX_LIST_PREV.search(text, max(0, start - 12), start)
    if p and p.group(1) != s and known(p.group(1)) and p.group(1) not in BLACKLIST:
        return True
    n = _RX_LIST_NEXT.match(text, end)
    return bool(n and n.group(1) != s and known(n.group(1)) and n.group(1) not in BLACKLIST)


def extract_spans(text: str, watchlist: list[str] | set[str] | None = None, limit: int = MAX_TICKERS,
                  social: bool = False) -> list[tuple[str, int, int]]:
    """[(ticker, start, end)] of US tickers mentioned in `text`, in order of first appearance (max `limit`).
    `social` relaxes the evidence needed for bare uppercase tokens (Reddit / StockTwits text)."""
    if not text:
        return []
    _compile()
    text = text.translate(_APOS)
    u = universe()
    known = (lambda s: s in u) if u else (lambda s: s in _curated_tickers)
    watch = {normalize_symbol(w) for w in (watchlist or ())} - {""}
    found: dict[str, tuple[int, int]] = {}

    def add(sym: str, pos: int, end: int | None = None) -> None:
        if sym and (sym not in found or pos < found[sym][0]):
            found[sym] = (pos, end if end is not None else pos + len(sym))

    crypto = None
    for m in _RX_CASHTAG.finditer(text):
        s = normalize_symbol(m.group(1))
        if s and (known(s) or s in watch or (not u and m.group(1).isupper())):
            add(s, m.start(), m.end())
    for rx in (_RX_EXCH, _RX_TICKER_EXCH, _RX_LISTED, _RX_SYMBOL_PHRASE):
        for m in rx.finditer(text):
            s = normalize_symbol(m.group(1))
            if s and (rx is _RX_EXCH or rx is _RX_TICKER_EXCH or s not in BLACKLIST):
                add(s, m.start(1), m.end(1))
    for m in _RX_PARENS.finditer(text):
        prev, raw = m.group(1), m.group(2)
        if not prev[0].isupper():
            continue
        s = normalize_symbol(raw)
        if s and not known(s):
            s = normalize_symbol(_RIC_SUFFIX.sub("", raw))
        if not (s and known(s) and s not in BLACKLIST):
            continue
        if s not in watch:
            # "PancakeSwap (CAKE)": a capitalized word before the parentheses must look like the company name.
            before = _RX_ANY_WORD.findall(text[max(0, m.start(2) - 80):m.start(2)])[-4:]
            caps = [w for w in before if _RX_CAP_WORD.fullmatch(w)]
            if crypto is None:
                crypto = bool(_RX_CRYPTO_CTX.search(text))
            if (s not in _curated_tickers or crypto) and not _shares_prefix(caps, _name_words(s)):
                continue
        add(s, m.start(2), m.end(2))
    upper = _mostly_upper(text)
    spans = [] if upper else _generic_names(text, add)
    hit_cs = _names_cs.keys() & set(_RX_TOKEN.findall(text))
    hit_ci = _names_ci.keys() & set(_RX_TOKEN.findall(text.lower()))
    skip_until = 0
    for t in (_RX_TOKEN.finditer(text) if hit_cs or hit_ci else ()):
        tok = t.group(0)
        low_tok = tok.lower()
        if tok not in hit_cs and low_tok not in hit_ci:
            continue
        if t.start() < skip_until or any(a <= t.start() < b for a, b in spans):
            continue
        for rx in tuple(_names_cs.get(tok) or ()) + tuple(_names_ci.get(low_tok) or ()):
            m = rx.match(text, t.start())
            if not m:
                continue
            key = re.sub(r"\s+", " ", m.group(1)).lower()
            sym = _alias_ticker.get(key)
            if not sym:
                continue
            skip_until = m.end()
            spans.append((m.start(), m.end(1)))
            if sym == "_" or (key in _alias_broker and _broker_context(text, m.start(), m.end())):
                break
            add(sym, m.start(), m.end(1))
            break
    if not upper and _one_tok:
        for m in _RX_ONE_TOK.finditer(text):
            sym = _one_tok.get(m.group(1))
            if sym and not any(a <= m.start() < b for a, b in spans):
                add(sym, m.start(), m.end(1))
                spans.append((m.start(), m.end(1)))
    if upper:
        for m in _RX_BARE.finditer(text):
            s = m.group(1)
            if s in watch and len(s) >= 3 and s not in BLACKLIST:
                add(s, m.start(), m.end())
    else:
        for m in _RX_BARE.finditer(text):
            s = m.group(1)
            if _in_caps_run(text, m.start(), m.end(), known):
                continue
            if s in BLACKLIST:
                # Stoplisted abbreviations ("MTD" = month-to-date) only with clear stock wording around them.
                if (known(s) and not is_etf(s) and not any(a <= m.start() < b for a, b in spans)
                        and _blacklisted_stock_ctx(text, m.start(), m.end())):
                    add(s, m.start(), m.end())
                continue
            if any(a <= m.start() < b for a, b in spans):
                continue                               # part of a matched company name ("FTI Consulting")
            if s in _broker_syms and _broker_context(text, m.start(), m.end()):
                continue
            if s in watch:
                add(s, m.start(), m.end())
            elif len(s) >= 3 and known(s) and (not is_etf(s) or s in POPULAR_ETFS):
                if s in POPULAR_ETFS or _bare_ok(text, s, m.start(), m.end(), known,
                                                 not social and s not in _curated_tickers):
                    add(s, m.start(), m.end())
    out = sorted(found.items(), key=lambda kv: kv[1][0])[:limit]
    return [(s, a, b) for s, (a, b) in out]


def extract(text: str, watchlist: list[str] | set[str] | None = None, limit: int = MAX_TICKERS,
            social: bool = False) -> list[str]:
    """US tickers mentioned in `text`, in order of first appearance (max `limit`)."""
    return [s for s, _, _ in extract_spans(text, watchlist, limit, social)]


_GENERIC_FIRST = set("""american first united general national global international capital digital energy
financial new great north south east west central pacific atlantic royal premier advanced applied standard
""".split())


def mention_span(sym: str, text: str) -> tuple[int, int] | None:
    """Where `sym` (a source-provided tag) is named in `text`: the symbol itself, a curated alias or the
    universe company name (full, or its distinctive first word). None when it is not mentioned."""
    if not text or not sym:
        return None
    _compile()
    text = text.translate(_APOS)
    m = re.search(r"(?<![\w$])\$?" + re.escape(sym) + r"(?![\w])", text)
    best = (m.start(), m.end()) if m else None
    for rx in _sym_rx.get(sym) or ():
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(1))
    v = universe().get(sym)
    if v:
        toks = _name_tokens(v[0])
        cands = [toks] if len(toks) >= 2 else []
        if toks and len(toks[0]) >= 4 and toks[0].lower() not in _GENERIC_FIRST and toks[0].lower() not in _LEGAL:
            cands.append(toks[:1])
        for c in cands:
            m = re.search(r"(?<![\w])" + r"\s+".join(re.escape(t) for t in c) + r"(?:'s)?(?![\w\-])", text)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), m.end())
                break
    return best


__all__ = ["refresh_symbols", "extract", "extract_spans", "mention_span", "company_name", "normalize_symbol",
           "universe", "is_known", "is_etf", "POPULAR_ETFS", "BLACKLIST", "SYMBOLS_PATH"]
