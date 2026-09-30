"""Avto-topish: o'zbek auditoriyali faol guruhlarni qidirish, qonuniylik filtri, kunlik a'zo bo'lish, reklama uchun tayyorlash.

Filtr yordamchi vosita: u xavfli mavzularni (diniy, zo'ravonlik, 18+, qimor, siyosat, firibgarlik va h.k.) kalit so'zlar bo'yicha
aniqlaydi. Yuz foiz kafolat bermaydi; gumonli guruhlar avtomatik a'zo bo'linmaydi, qo'lda ko'rib chiqiladi."""
import asyncio
import json
import random
import re
from datetime import datetime, timedelta

from . import db
from .config import log
from .core import joiner, manager
from .limits import blacklisted_ids, warm_today, work_gate

# ---------------- kalit so'zlar (qidirish va mosligi) ----------------
DEFAULT_KEYWORDS = {
    "Dasturlash / IT": ["dasturlash", "dasturchilar", "python uz", "it uz", "web dasturlash", "frontend uz", "backend uz",
                        "programmer uzbekistan", "kodlash", "it kurslar", "sun'iy intellekt", "telegram bot yasash"],
    "O'quv markazlari": ["o'quv markaz", "kurslar toshkent", "ta'lim markazi", "til kurslari", "repetitor", "online kurs uz",
                         "ingliz tili kurs", "o'qituvchilar"],
    "Universitet / maktab / bog'cha": ["universitet uz", "talabalar", "texnikum", "kollej", "maktab o'qituvchilari",
                                       "bog'cha tarbiyachilar", "abituriyent", "ota-onalar guruhi", "oliy ta'lim"],
    "Biznes": ["biznes uz", "tadbirkorlar", "tadbirkorlik", "savdo uzbekistan", "startap uz", "marketing uz", "smm uz",
               "onlayn savdo", "biznes hamjamiyat", "hamkorlik biznes"],
    "Koreys tili": ["koreys tili", "koreys tili o'rganish", "koreys tili online", "koreys tili kursi", "topik", "eps topik",
                    "koreyada ish", "koreyada o'qish", "koreya vizasi", "janubiy koreya o'zbeklar", "koreya o'zbeklari", "kpop uzbek", "kdrama uzbek"],
    "Hudud: Qoraqalpog'iston / Xorazm": ["Nukus", "Nukus maktab", "Nukus universitet", "Nukus talabalar", "Nukus o'quv markaz",
                                         "Qoraqalpog'iston", "Qoraqalpog'iston talabalar", "Qoraqalpog'iston ta'lim", "Xorazm",
                                         "Xorazm maktab", "Xorazm universitet", "Urganch", "Urganch talabalar", "Urganch o'quv markaz",
                                         "Xiva", "Nukus koreys tili", "Xorazm koreys tili", "Qaraqalpaqstan", "Нукус", "Ургенч"],
    "Avtomatlashtirish": ["avtomatlashtirish", "crm uz", "biznes avtomatlashtirish", "1c uz", "chat bot biznes",
                          "raqamli marketing", "onlayn do'kon", "sotuv avtomatlashtirish"],
}

# Koreys tili kurslari reklamasi uchun tayyor auditoriya so'zlari (Guruh qidirish sahifasida chip sifatida chiqadi)
KOREAN_PRESETS = {
    "Koreys tilini o'rganish": ["koreys tili", "koreys tili o'rganish", "koreys tili online", "koreys tili kursi", "koreys tili darslari",
                                "koreys tili offline", "koreys tili toshkent", "koreys tili boshlang'ich", "hangul", "koreys alifbosi",
                                "koreys tili lug'at", "koreys tili o'zbek tilida", "koreys tili guruh"],
    "TOPIK / EPS-TOPIK": ["topik", "topik 1", "topik 2", "eps topik", "eps topik imtihon", "topik tayyorgarlik", "topik uzbekistan",
                          "koreys tili imtihon", "topik testlar", "eps topik darslik"],
    "Koreyada ish va viza": ["koreyada ish", "koreyaga ketish", "koreya ish e'lonlari", "koreya vizasi", "e-9 viza", "koreya ishchilar",
                             "janubiy koreya o'zbeklar", "koreya o'zbeklari", "koreyada yashash", "koreya ishga borish"],
    "Koreyada o'qish": ["koreyada o'qish", "koreya universitetlari", "gks stipendiya", "kgsp", "koreya grant", "koreya stipendiya",
                        "koreyaga o'qishga kirish", "koreya talabalari uzbek", "koreya til markazi"],
    "Koreys madaniyati": ["kpop uzbek", "k-pop", "kdrama uzbek", "koreys seriallari", "koreys seriallari o'zbek tilida", "koreys madaniyati",
                          "koreys filmlari uzbek", "bts uzbek", "koreya kosmetika", "koreys taomlari"],
    "Ruscha / kirillcha": ["корейский язык ташкент", "корейский язык онлайн", "курсы корейского языка", "изучение корейского языка",
                           "корейский язык для начинающих", "работа в корее", "topik узбекистан", "корея узбеки", "корейский язык узбекистан"],
}

# Ta'lim muassasalari uchun tayyor so'zlar
EDU_PRESETS = {
    "Universitet / OTM / institut": [
        "universitet", "oliy ta'lim muassasasi", "OTM talabalari", "institut talabalari", "talabalar guruhi", "talabalar chat",
        "1-kurs talabalari", "2-kurs talabalari", "3-kurs talabalari", "4-kurs talabalari", "bitiruvchi kurs", "abituriyent",
        "abituriyentlar 2026", "DTM", "DTM test", "qabul 2026", "magistratura", "bakalavr", "kontrakt talabalar", "grant talabalar",
        "stipendiya", "talabalar yotoqxonasi", "universitet e'lonlari", "universitet talabalari", "xalqaro universitet",
        "pedagogika universiteti", "tibbiyot universiteti", "texnika universiteti", "iqtisodiyot universiteti", "davlat universiteti",
        "TATU", "TDIU", "TDYU", "SamDU", "NamDU", "AndDU", "FarDU", "BuxDU", "UrDU", "QDU", "NDPI", "university uzbekistan"],
    "Maktab": [
        "maktab", "maktab ota-onalar", "sinf ota-onalar guruhi", "1-sinf ota-onalar", "5-sinf ota-onalar", "9-sinf ota-onalar",
        "11-sinf bitiruvchilar", "maktab o'qituvchilari", "maktab direktorlari", "maktab e'lonlari", "ixtisoslashtirilgan maktab",
        "prezident maktabi", "IDUM", "sinf rahbarlari", "boshlang'ich sinf o'qituvchilari", "matematika o'qituvchilari",
        "ona tili o'qituvchilari", "ingliz tili o'qituvchilari", "maktab olimpiada", "olimpiada tayyorgarlik", "bitiruvchilar 2026",
        "o'quvchilar guruhi", "umumiy o'rta ta'lim maktabi", "xalq ta'limi", "o'qituvchilar"],
    "Bog'cha / MTT": [
        "bog'cha", "bog'cha tarbiyachilar", "bog'cha ota-onalar", "maktabgacha ta'lim", "maktabgacha ta'lim tashkiloti", "MTT",
        "MTT tarbiyachilari", "bolalar bog'chasi", "bog'cha direktorlari", "davlat bog'chasi", "xususiy bog'cha", "tarbiyachi va enagalar",
        "maktabga tayyorlov", "bog'cha metodistlar", "ota-onalar bog'cha guruhi"],
    "Texnikum / kollej": [
        "texnikum", "kollej", "kasb-hunar kolleji", "politexnikum", "texnikum talabalari", "kollej talabalari", "texnikum abituriyent",
        "pedagogika kolleji", "tibbiyot texnikumi", "iqtisodiyot texnikumi", "moliya kolleji", "agrar texnikum", "avtomobil yo'llari texnikumi",
        "kasb-hunar maktabi", "texnikum qabul", "kollej qabul 2026", "texnikum yotoqxona", "IT kollej", "raqamli texnologiyalar texnikumi",
        "texnikum bitiruvchilari", "kollej guruhi", "texnikum e'lonlari"],
    "O'quv markaz / kurslar": [
        "o'quv markaz", "ta'lim markazi", "o'quv kurslari", "repetitor", "til kurslari", "IT kurslar", "abituriyent kurslar",
        "tayyorlov kurslari", "bilim markazi", "onlayn ta'lim", "masofaviy ta'lim", "o'qituvchilar guruhi", "ingliz tili kurslari",
        "rus tili kurslari", "matematika kursi", "dasturlash kursi", "kasb o'rgatish kurslari", "buxgalteriya kursi", "SMM kursi"],
}

# Qoraqalpog'iston, Xorazm, Nukus tomonlari uchun qidiruv so'zlari (joy nomi x mavzu + qoraqalpoq/rus tilidagi variantlar)
_PLACES = ["Nukus", "Qoraqalpog'iston", "Xorazm", "Urganch", "Xiva"]
_TOPICS = ["maktab", "universitet", "talabalar", "kollej", "texnikum", "bog'cha", "o'quv markaz", "ta'lim", "mahalla", "yoshlar",
           "ish e'lonlari", "tadbirkorlar", "onlayn savdo", "ota-onalar", "o'qituvchilar"]
REGION_PRESETS = {
    "Nukus": [f"Nukus {t}" for t in _TOPICS] + ["Nukus", "Nukus chat", "Nukus yangiliklari", "Nukus davlat pedagogika instituti", "Nukus filiali"],
    "Qoraqalpog'iston": [f"Qoraqalpog'iston {t}" for t in _TOPICS] + ["Qoraqalpog'iston", "Qoraqalpog'iston yangiliklari", "Qoraqalpog'iston respublikasi",
                                                                    "Qoraqalpoq davlat universiteti", "Qoraqalpoq talabalari", "Qoraqalpoq yoshlari"],
    "Xorazm / Urganch / Xiva": [f"{p} {t}" for p in ("Xorazm", "Urganch", "Xiva") for t in _TOPICS[:11]] +
                               ["Urganch davlat universiteti", "Xorazm Ma'mun akademiyasi", "Xorazm viloyati", "Urganch chat", "Xiva chat"],
    "Tumanlar": [f"{t} {topic}" for t in ("Shovot", "Xonqa", "Bog'ot", "Qo'shko'pir", "Yangiariq", "Yangibozor", "Gurlan", "Hazorasp",
                                         "Tuproqqal'a", "Xo'jayli", "To'rtko'l", "Beruniy", "Qo'ng'irot", "Mo'ynoq", "Chimboy", "Kegeyli",
                                         "Shumanay", "Amudaryo", "Ellikqal'a", "Taxtako'pir", "Bo'zatov", "Qanliko'l", "Taxiatosh")
                 for topic in ("chat", "maktab")],
    "Qoraqalpoq tilida (lotin)": ["Qaraqalpaqstan", "Qaraqalpaq", "Nokis", "Nokis mektep", "Nokis universitet", "Qaraqalpaq tili",
                                  "Qaraqalpaqstan janaliqlari", "mektep", "mektep oqiwshilari", "mugallimler", "bilimlendiriw",
                                  "oqiw orayi", "oqiw orayi Nokis", "studentler", "kollej Nokis", "balalar baqshasi", "jumis Nokis",
                                  "Qaraqalpaqstan jumis", "Qaraqalpaqstan jaslari", "koreys tili Nokis"],
    "Ruscha / kirillcha": ["Нукус", "Нукус чат", "Нукус школа", "Нукус университет", "Нукус учебный центр", "Нукус колледж", "Нукус работа",
                           "Каракалпакстан", "Каракалпакстан новости", "Каракалпакстан студенты", "Нөкис", "Қарақалпақстан",
                           "Ургенч", "Ургенч университет", "Ургенч школа", "Ургенч работа", "Хорезм", "Хорезм студенты", "Хива"],
}

# Sahifadagi "tayyor so'zlar" bo'limlari: sarlavha -> (guruhlar, Avto-topishdagi kategoriya)
PRESET_SETS = {
    "Koreys tili kurslari": (KOREAN_PRESETS, "Koreys tili"),
    "Ta'lim muassasalari": (EDU_PRESETS, "Universitet / maktab / bog'cha"),
    "Qoraqalpog'iston, Xorazm, Nukus": (REGION_PRESETS, "Hudud: Qoraqalpog'iston / Xorazm"),
}

# Standart taqiq ro'yxati (foydalanuvchi sahifada tahrirlashi mumkin). So'z boshidan moslik: "islom" -> "islomiy" ham topiladi.
DEFAULT_BANS = {
    "Diniy": [
        "islom$", "islamiy", "diniy", "din ", "dinshunos", "namoz", "qur'on", "quron", "qur’on", "hadis", "sunnat", "payg'ambar", "paygambar",
        "imom$", "masjid", "madrasa", "voiz", "va'z", "vaz ", "ustoz din", "haj ", "umra", "zikr", "tavhid", "iymon", "imon ", "shariat",
        "fatvo", "halol nikoh", "ramazon", "ro'za", "ruza ", "duo ", "dualar", "allohning", "alloh", "muslim$", "muslima", "hijob", "niqob",
        "sahoba", "tafsir", "tasavvuf", "sufiy", "xutba", "azon", "jannat", "do'zax", "oxirat", "bid'at", "mazhab", "mazhabsiz",
        "ислом$", "исломий", "дин ", "динй", "диний", "намоз", "қуръон", "куръон", "қур'он", "хадис", "суннат", "пайғамбар", "имом$", "масжид",
        "мадраса", "воиз", "ваъз", "шариат", "фатво", "рамазон", "рўза", "дуо", "аллоҳ", "аллох", "мусулм", "ҳижоб", "хутба", "жаннат",
        "ислам$", "мусульман", "коран", "хадис", "намаз", "мечеть", "имам$", "проповед", "шариат", "фетва", "рамадан", "хиджаб", "сунна",
        "христиан", "библи", "церков", "православ", "евангел", "пастор", "секта", "иегов", "буддизм", "кришна",
        "islamic", "muslim", "quran", "koran", "sermon", "church", "bible", "christian", "prayer group", "sharia", "hijab", "imam",
    ],
    "Da'vat / mazhab / sekta": [
        "da'vat", "davat", "da’vat", "tablig", "tabligh", "jamoat da'vat", "missioner", "prozelit", "targ'ibot din", "nasroniy",
        "yahovo", "sekta", "sektant", "kult ", "mazhabiy", "tariqat", "salafiy", "vahhobiy", "vahhobi", "hizb", "hizbut", "akromiy", "nurchi",
        "тарғибот", "даъват", "давъат", "таблиг", "миссионер", "секта", "сектант", "салафи", "ваххаб", "хизб", "акромий", "нурчи", "тариқат",
        "даават", "призыв к вере", "обращение в веру", "wahhab", "salafi", "hizb ut", "da'wah", "dawah", "proselyt",
    ],
    "Zo'ravonlik / ekstremizm": [
        "zo'ravon", "zo‘ravon", "zoravon", "qurol", "qurolli", "o'ldir", "oldir ", "terror", "ekstrem", "radikal", "qotil", "qotillik", "jang video",
        "qon to'k", "jihod", "xalifat", "mujohid", "shahid$", "urush video", "qiynoq", "kaltak", "bezorilik", "banda", "reket", "tajovuz",
        "zo'rlash", "zorlash", "o'ch olish", "qasos", "granata", "avtomat$", "автомат$", "pistolet", "portlat", "bomba", "mina ", "miltiq",
        "зўравон", "қурол", "ўлдир", "террор", "экстрем", "радикал", "қотил", "жиҳод", "халифат", "мужоҳид", "шаҳид$", "қийноқ", "портлат",
        "убий", "убить", "оружи", "насили", "терро", "экстрем", "радикал", "джихад", "халифат", "моджахед", "шахид$", "пытк", "взрыв", "граната",
        "бомб", "пистолет", "расправа", "драки", "избиени", "бандит", "криминал", "мафи", "рэкет", "изнасил",
        "weapon", "violence", "violent", "terror", "extremis", "radical", "gore", "murder", "killer", "kill ", "jihad", "caliphate", "torture", "bomb", "gun ", "firearm", "shooting video", "fight video",
    ],
    "18+ / axloqsiz": [
        "18+", "18 +", "+18", "porno", "erotik", "erotika", "seks", "intim", "intimniy", "escort", "eskort", "xxx", "nude", "nudes",
        "yalang'och", "yalangoch", "tanishuv", "tanishish uchun", "uchrashuv qiz", "ochiq qiz", "fohisha", "prostitut", "kanizak", "striptiz",
        "onlyfans", "webcam", "hot ", "sexy", "seksual", "gey ", "gay ", "lgbt", "lesbi", "transgender", "shaxvat",
        "порно", "эротик", "секс", "интим", "эскорт", "голые", "обнаж", "знакомств", "проститут", "стриптиз", "вебкам", "виртуал секс",
        "шаҳват", "эротика", "танишув", "фоҳиша", "лгбт", "гей ", "лесби", "трансгендер",
        "sex", "porn", "erotic", "hookup", "adult", "nsfw", "onlyfans", "dating", "escort", "strip", "hentai", "fetish",
    ],
    "Qimor / stavka": [
        "qimor", "qimorbozlik", "kazino", "casino", "stavka", "stavkalar", "1xbet", "1хbet", "melbet", "linebet", "mostbet", "pin-up", "pinup", "parimatch",
        "betting", "bookmaker", "букмекер", "ставк", "казино", "азарт", "лотере", "лото", "рулетка", "слоты", "слот ", "покер", "тотализатор",
        "lotereya", "lotto", "bukmeker", "slot ", "poker", "ruletka", "totalizator", "aviator", "jekpot", "jackpot", "yutuq o'yin", "tikish",
        "қимор", "казино", "ставка", "лотерея", "авиатор", "1хбет", "промокод ставк",
    ],
    "Firibgarlik / piramida": [
        "piramida", "tez boyish", "tez pul", "oson pul", "pul ishlash oson", "kafolatli daromad", "kafolatli foyda", "100% foyda", "yopiq investitsiya",
        "investitsiya kafolat", "pul ikki barobar", "pul ko'paytirish", "pul yuvish", "obnal", "обнал", "moliyaviy piramida", "xayriya sovg'a", "sovg'a yutib",
        "yutuq yutdingiz", "soxta", "fake", "soxta hujjat", "diplom sotib", "diplom olib", "spravka sotib", "hujjat yasash", "litsenziya sotib",
        "kredit olish", "onlayn kredit", "mikrokredit", "zaym", "займ", "микрозайм", "быстрые деньги", "легкий заработок", "лёгкий заработок",
        "финансовая пирамида", "гарантированный доход", "удвоение", "заработок без вложений", "пассивный доход", "подделка документов", "купить диплом",
        "купить справк", "обналичив", "отмыв", "пирамида", "лохотрон", "развод", "кидалов", "аферист",
        "пирамида", "тез бойиш", "тез пул", "осон пул", "кафолатли даромад", "сохта ҳужжат",
        "ponzi", "scam", "fraud", "get rich", "easy money", "double your", "guaranteed profit", "fake diploma", "fake document", "carding", "cvv", "dump ",
    ],
    "Giyohvand / alkogol / tamaki": [
        "narkotik", "giyohvand", "giyoh", "mefedron", "geroin", "kokain", "marixuana", "marijuana", "gashish", "spays", "spice", "sintetik", "zakladka", "zakladchik",
        "alkogol", "spirtli", "aroq", "vodka", "pivo ", "vino ", "sigaret", "vape", "vayp", "kalyan", "nasvay", "nos ", "tamaki", "qamish",
        "наркот", "наркоман", "закладк", "закладчик", "меф", "героин", "кокаин", "марихуан", "гашиш", "спайс", "соль ", "амфетамин", "экстази",
        "алкогол", "водк", "пиво", "вино ", "сигарет", "вейп", "кальян", "насвай", "табак",
        "нарко", "гиёҳванд", "ароқ", "нос ", "наркотик",
        "drugs", "weed", "cocaine", "heroin", "meth ", "lsd", "mdma", "cannabis", "alcohol", "vodka", "cigarette", "vape", "hookah", "tobacco",
    ],
    "Siyosat / qonunga zid": [
        "siyosat", "siyosiy", "muxolifat", "muxolif", "norozilik", "mitin", "namoyish", "inqilob", "to'ntarish", "davlatga qarshi", "hukumatga qarshi",
        "prezident tanqid", "prezidentga qarshi", "sayloy", "saylov kampaniya", "referendum", "separatiz", "avtonomiya", "chegara nizo", "sanksiya",
        "propaganda", "targ'ibot siyosiy", "ochiq siyosat", "opozitsiya", "boykot", "davlat sirlari", "mafkuraviy", "ekstremistik material",
        "сиёсат", "сиёсий", "мухолиф", "норозилик", "митинг", "намойиш", "инқилоб", "тўнтариш", "референдум", "сепаратизм", "пропаганда",
        "политик", "политика", "оппозиц", "протест", "митинг", "революци", "переворот", "антиправительств", "против власти", "выборы", "санкции",
        "сепаратизм", "пропаганда", "госизмен", "экстремистск",
        "politics", "political", "opposition", "protest", "rally", "revolution", "coup", "anti-government", "separatis", "propaganda", "sanction",
    ],
    "Gumonli / xavfli": [
        "hacking", "xakerlik", "xaker", "hack qilish", "akkaunt buzish", "akkaunt oldi", "parol buzish", "buzib kirish", "ddos", "botnet", "spam bot", "spamer",
        "raqam yig'ish", "baza sotib", "baza sotaman", "telefon bazasi", "shaxsiy ma'lumot sotish", "ma'lumotlar bazasi sotish", "leak", "sliv", "doxing",
        "хакер", "хакинг", "взлом", "взломать", "пробив", "пробить", "слив", "утечка", "база данных купить", "база номеров", "спам", "спамер", "ботнет", "ддос",
        "хакерлик", "бузиб кириш",
        "kripto", "crypto", "bitcoin", "bitkoin", "usdt", "trc20", "airdrop", "nft ", "binance signal", "forex", "foreks", "treyding", "trading", "signal guruh", "signallar",
        "binar", "binary option", "opsion savdo", "криптовалют", "крипто", "биткоин", "форекс", "трейдинг", "сигналы", "бинарн", "опцион", "обмен валют",
        "форекс", "трейдинг",
        "mlm", "network marketing", "tarmoq marketing", "referal daromad", "referal", "passiv daromad", "passiv pul", "biznes taklif kafolat",
        "млм", "сетевой маркетинг", "реферал",
        "vpn", "vpn ", "proksi", "proxy", "anonim", "anonimayzer", "tor browser", "yashirin kanal", "yashirin guruh", "yopiq kanal", "vless", "shadowsocks", "впн", "прокси", "анонимайзер", "обход блокировк",
        "pul o'tkazma", "pul otkazma", "yutuq", "sovg'a yutib", "konkurs pul", "sim karta sotib", "sim karta sotaman", "karta ijara", "kartani ijaraga", "karta sotaman",
        "аренда карт", "продам карт", "сим карт", "дроп$", "дропы$", "drop karta",
    ],
}
def _norm(d):
    """'so'z ' (oxirida bo'shliq) -> 'so'z$' (butun so'z)."""
    return {c: [(w.strip() + "$") if w.endswith(" ") else w for w in ws] for c, ws in d.items()}


DEFAULT_BANS = _norm(DEFAULT_BANS)
# eski nom (moslik uchun)
BLOCK = DEFAULT_BANS

UZ_LAT = ["va", "bilan", "uchun", "kerak", "bor", "yo'q", "yoq", "salom", "assalomu", "qanday", "narx", "kurs", "dars", "qiling",
          "mumkin", "bo'lsa", "ham", "emas", "iltimos", "rahmat", "nima", "qancha", "kim", "bu", "shu", "men", "siz", "bo'ladi",
          "kerakmi", "yozing", "murojaat", "aloqa", "guruh", "xabar", "savol", "javob", "yordam", "ish", "toshkent", "o'zbek"]
UZ_CYR = ["ва", "билан", "учун", "керак", "бор", "йўқ", "салом", "қандай", "нарх", "дарс", "мумкин", "ҳам", "эмас", "илтимос",
          "раҳмат", "нима", "қанча", "ким", "бу", "шу", "мен", "сиз", "ўзбек", "ишлаш", "гуруҳ"]
RU = ["это", "что", "как", "для", "или", "вы", "мы", "они", "если", "когда", "очень", "здравствуйте", "спасибо", "пожалуйста",
      "нужно", "можно", "есть", "нет", "работа", "цена", "курс", "группа", "привет", "который", "тоже", "уже"]
EN = ["the", "and", "you", "for", "with", "this", "that", "are", "have", "how", "what", "please", "thanks", "hello"]
UZ_CYR_LETTERS = set("қғўҳҚҒЎҲ")


def keywords_for(aid: int) -> dict:
    r = db.one("SELECT keywords_json FROM autojoin WHERE account_id=?", (aid,))
    try:
        kw = json.loads(r["keywords_json"]) if r and r["keywords_json"] else None
    except Exception:
        kw = None
    return kw or DEFAULT_KEYWORDS


def bans_for(row) -> dict:
    try:
        b = json.loads(row["ban_json"]) if row and row["ban_json"] else None
    except Exception:
        b = None
    return b if isinstance(b, dict) else {k: list(v) for k, v in DEFAULT_BANS.items()}


def get_cfg(aid: int) -> dict:
    r = db.one("SELECT * FROM autojoin WHERE account_id=?", (aid,))
    if not r:
        db.ex("INSERT INTO autojoin(account_id) VALUES(?)", (aid,))
        r = db.one("SELECT * FROM autojoin WHERE account_id=?", (aid,))
    d = dict(r)
    d["keywords"] = keywords_for(aid)
    d["bans"] = bans_for(r)
    d["block_extra_raw"] = r["block_extra"] or ""
    d["block_extra"] = [x.strip().lower() for x in (r["block_extra"] or "").splitlines() if x.strip()]
    return d


def _hits(text: str, words) -> list[str]:
    out = []
    for w in words:
        whole = w.endswith("$")
        pat = r"(?<![\w'`ʻ’])" + re.escape(w[:-1] if whole else w) + (r"(?![\w'`ʻ’])" if whole else "")
        if re.search(pat, text):
            out.append(w)
    return out


def uz_share(texts: list[str]) -> tuple[int, int]:
    """(o'zbek tili ulushi %, tahlil qilingan so'zlar soni)."""
    blob = " ".join(texts).lower()
    words = re.findall(r"[\w'`ʻ’]+", blob)
    if not words:
        return 0, 0
    setl, setc, setr, sete = set(UZ_LAT), set(UZ_CYR), set(RU), set(EN)
    uz = sum(1 for w in words if w.replace("ʻ", "'").replace("’", "'") in setl or w in setc)
    uz += 3 * sum(1 for ch in blob if ch in UZ_CYR_LETTERS) // 3
    ru = sum(1 for w in words if w in setr)
    en = sum(1 for w in words if w in sete)
    tot = uz + ru + en
    return (round(uz * 100 / tot) if tot else 0), len(words)


def classify(info: dict, cfg: dict, keyword: str = "") -> dict:
    """Guruhga hukm chiqaradi: ready / review / blocked / low / skip."""
    title, about = info.get("title") or "", info.get("about") or ""
    texts = info.get("texts") or []
    head = f"{title} {about}".lower()
    body = " ".join(texts).lower()
    everything = head + " " + body
    res = {"status": "ready", "reason": "", "category": "", "uz": 0, "score": 0.0}
    if info.get("skip"):
        return dict(res, status="skip", reason=info["skip"])
    if info.get("joined"):
        return dict(res, status="skip", reason="Allaqachon a'zo")
    if info.get("scam") or info.get("restricted"):
        return dict(res, status="blocked", reason="Telegram bu guruhni cheklagan (scam/fake/restricted)")
    for cat, words in cfg["bans"].items():
        h = _hits(everything, words)
        if h:
            return dict(res, status="blocked", reason=f"{cat}: '{h[0].rstrip('$')}' topildi")
    extra = _hits(everything, cfg["block_extra"])
    if extra:
        return dict(res, status="blocked", reason=f"Sizning taqiq so'zingiz: '{extra[0]}'")
    # mavzuga moslik
    cat = ""
    for c, words in cfg["keywords"].items():
        if _hits(head, [w.split()[0] for w in words]) or _hits(head, [w for w in words]):
            cat = c
            break
    res["category"] = cat
    uz, wc = uz_share(texts + [title, about])
    res["uz"] = uz
    from .auditor import get_cfg as _acfg
    min_members = max(cfg["min_members"], _acfg(cfg["account_id"])["min_members"])
    members, per_day = info.get("members") or 0, info.get("per_day") or 0
    if info.get("ads_flag"):
        return dict(res, status="low", reason="Guruhda reklama taqiqlangan (tavsif/mahkamlangan xabar)")
    if not info.get("can_send", True):
        return dict(res, status="low", reason="Guruhda a'zolar yoza olmaydi")
    if members < min_members:
        return dict(res, status="low", reason=f"A'zolar kam ({members} < {min_members})")
    if per_day < cfg["min_per_day"]:
        return dict(res, status="low", reason=f"Faollik past (kuniga {per_day} xabar < {cfg['min_per_day']})")
    if wc >= 25 and uz < cfg["min_uz"]:
        return dict(res, status="low", reason=f"O'zbek tili ulushi past ({uz}%)")
    res["score"] = round(min(100, per_day) * 0.45 + min(100, members / 200) * 0.3 + uz * 0.25, 1)
    if wc < 25:
        return dict(res, status="review", reason="Matn kam: tilni aniqlab bo'lmadi (qo'lda ko'ring)")
    if info.get("join_request"):
        return dict(res, status="review", reason="A'zo bo'lish uchun admin tasdig'i kerak")
    if not cat:
        return dict(res, status="review", reason="Mavzu sizning kalit so'zlaringizga mos emas (qo'lda ko'ring)")
    if cfg["manual_all"]:
        return dict(res, status="review", reason="Hammasini qo'lda tasdiqlash yoqilgan")
    return res


# ---------------- qidirish ----------------
progress: dict[int, dict] = {}


async def search(aid: int, max_queries: int = 6, max_inspect: int = 25):
    cfg = get_cfg(aid)
    svc = manager.get(aid)
    kws = [(c, k) for c, ks in cfg["keywords"].items() for k in ks]
    if not kws:
        return 0
    prog = progress[aid] = {"running": True, "done": 0, "total": max_queries, "new": 0, "msg": "Qidirilmoqda"}
    ptr = cfg["kw_ptr"] or 0
    found = 0
    try:
        for i in range(max_queries):
            cat, kw = kws[(ptr + i) % len(kws)]
            try:
                results = await svc.search_public(kw)
            except Exception as e:
                log.warning("Qidiruv xatosi (%s): %s", kw, type(e).__name__)
                if type(e).__name__ == "FloodWaitError":
                    break
                results = []
            for r in results:
                if found >= max_inspect:
                    break
                if r.get("kind") != "group" or not r.get("username"):
                    continue
                un = r["username"].lower()
                if db.one("SELECT 1 FROM disc_candidates WHERE account_id=? AND username=?", (aid, un)):
                    continue
                await asyncio.sleep(random.uniform(2, 4))
                try:
                    info = await svc.inspect_public(un)
                except Exception as e:
                    log.info("Tekshirib bo'lmadi %s: %s", un, type(e).__name__)
                    if type(e).__name__ == "FloodWaitError":
                        raise
                    continue
                if info.get("tg_id") in blacklisted_ids(aid):
                    continue
                v = classify(info, cfg, kw)
                db.ex("INSERT OR IGNORE INTO disc_candidates(account_id,username,title,about,members,per_day,uz,category,keyword,"
                      "score,status,reason,found_at,tg_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, un, info.get("title") or r.get("title"), (info.get("about") or "")[:300], info.get("members"),
                       info.get("per_day"), v["uz"], v["category"] or cat, kw, v["score"], v["status"], v["reason"], db.now(),
                       info.get("tg_id")))
                found += 1
                prog["new"] += 1
            prog["done"] = i + 1
            await asyncio.sleep(random.uniform(8, 15))
    except Exception as e:
        prog["msg"] = f"Xato: {type(e).__name__}"
    finally:
        db.ex("UPDATE autojoin SET kw_ptr=?, last_search=? WHERE account_id=?", ((ptr + max_queries) % max(1, len(kws)), db.now(), aid))
        prog.update(running=False, msg=prog.get("msg") if prog.get("msg", "").startswith("Xato") else "Tugadi")
    return found


# ---------------- a'zo bo'lish rejasi ----------------
def recheck(aid: int) -> int:
    """Taqiq ro'yxati o'zgargach, hali a'zo bo'linmagan nomzodlarni nom/tavsif bo'yicha qayta tekshiradi."""
    cfg = get_cfg(aid)
    n = 0
    for c in db.q("SELECT * FROM disc_candidates WHERE account_id=? AND status IN ('ready','review','approved','low')", (aid,)):
        head = f"{c['title'] or ''} {c['about'] or ''}".lower()
        for cat, words in cfg["bans"].items():
            h = _hits(head, words)
            if h:
                db.ex("UPDATE disc_candidates SET status='blocked', reason=? WHERE id=?", (f"{cat}: '{h[0].rstrip('$')}' topildi", c["id"]))
                n += 1
                break
        else:
            h = _hits(head, cfg["block_extra"])
            if h:
                db.ex("UPDATE disc_candidates SET status='blocked', reason=? WHERE id=?", (f"Sizning taqiq so'zingiz: '{h[0]}'", c["id"]))
                n += 1
    return n


def test_text(aid: int, text: str) -> list[dict]:
    cfg = get_cfg(aid)
    t = (text or "").lower()
    out = [{"cat": c, "word": w.rstrip("$")} for c, ws in cfg["bans"].items() for w in _hits(t, ws)]
    out += [{"cat": "Mening so'zlarim", "word": w} for w in _hits(t, cfg["block_extra"])]
    return out


def joined_today(aid: int) -> int:
    t0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    return db.one("SELECT COUNT(*) c FROM join_targets jt JOIN join_batches b ON b.id=jt.batch_id WHERE b.account_id=? "
                  "AND b.filename LIKE 'Avto-topish%' AND jt.status IN ('joined','requested') AND jt.tried_at>=?", (aid, t0))["c"]


def plan_join(aid: int, force: bool = False) -> int | None:
    """Bugun uchun a'zo bo'lish paketini yaratadi (bitta paket tugamaguncha ikkinchisi yaratilmaydi)."""
    cfg = get_cfg(aid)
    if db.one("SELECT 1 FROM join_batches WHERE account_id=? AND filename LIKE 'Avto-topish%' AND status IN ('queued','running','waiting')", (aid,)):
        return None
    quota = cfg["daily_target"] - joined_today(aid)
    w = warm_today(aid)
    if w:
        quota = min(quota, w["joins"] - joined_today(aid))
    if quota <= 0:
        return None
    st = "('approved')" if cfg["manual_all"] else "('ready','approved')"
    rows = db.q(f"SELECT * FROM disc_candidates WHERE account_id=? AND status IN {st} ORDER BY score DESC LIMIT ?", (aid, quota))
    if not rows:
        return None
    bid = db.ex("INSERT INTO join_batches(account_id,filename,status,created_at,total,min_delay,max_delay,daily_limit) "
                "VALUES(?,?,?,?,?,?,?,?)", (aid, f"Avto-topish {datetime.now():%Y-%m-%d %H:%M}", "queued", db.now(), len(rows),
                                            cfg["min_delay"], cfg["max_delay"], max(cfg["daily_target"], 1)))
    db.many("INSERT INTO join_targets(batch_id,ref,kind,key) VALUES(?,?,?,?)", [(bid, "@" + r["username"], "username", r["username"]) for r in rows])
    db.many("UPDATE disc_candidates SET status='queued', batch_id=? WHERE id=?", [(bid, r["id"]) for r in rows])
    joiner.start(bid)
    return bid


def sync(aid: int):
    """Paket natijalarini nomzodlarga ko'chiradi; yangi a'zo bo'lingan guruhlarni teglaydi."""
    for c in db.q("SELECT * FROM disc_candidates WHERE account_id=? AND status='queued'", (aid,)):
        t = db.one("SELECT * FROM join_targets WHERE batch_id=? AND key=?", (c["batch_id"], c["username"]))
        if not t or t["status"] == "pending":
            continue
        if t["status"] in ("joined", "already", "requested"):
            st = "requested" if t["status"] == "requested" else "joined"
            ad = (datetime.now() + timedelta(days=get_cfg(aid)["ad_wait_days"])).strftime("%Y-%m-%d %H:%M:%S")
            db.ex("UPDATE disc_candidates SET status=?, joined_at=?, ad_at=? WHERE id=?", (st, t["tried_at"] or db.now(), ad, c["id"]))
        else:
            db.ex("UPDATE disc_candidates SET status='failed', reason=? WHERE id=?", (t["detail"] or t["status"], c["id"]))
    for c in db.q("SELECT * FROM disc_candidates WHERE account_id=? AND status='joined'", (aid,)):
        g = db.one("SELECT * FROM groups WHERE account_id=? AND lower(username)=?", (aid, c["username"]))
        if g and not db.one("SELECT 1 FROM group_tags WHERE account_id=? AND tg_id=? AND tag='avto'", (aid, g["tg_id"])):
            db.ex("INSERT OR IGNORE INTO group_tags(account_id,tg_id,tag) VALUES(?,?,'avto')", (aid, g["tg_id"]))
            db.ex("UPDATE disc_candidates SET tg_id=? WHERE id=?", (g["tg_id"], c["id"]))


async def activate_ready(aid: int):
    """Kutish muddati o'tgan guruhlarda qoidalarni tekshirib, reklama uchun 'avto-tayyor' teg beradi."""
    svc = manager.get(aid)
    for c in db.q("SELECT * FROM disc_candidates WHERE account_id=? AND status='joined' AND ad_at<=? AND tg_id IS NOT NULL", (aid, db.now())):
        try:
            info = await svc.group_info(c["tg_id"])
        except Exception:
            continue
        db.ex("UPDATE groups SET about=?, slowmode=?, no_media=?, no_links=?, ads_flag=?, checked_at=? WHERE account_id=? AND tg_id=?",
              (info["about"], info["slowmode"], info["no_media"], info["no_links"], info["ads_flag"], db.now(), aid, c["tg_id"]))
        g = db.one("SELECT can_post, ads_flag FROM groups WHERE account_id=? AND tg_id=?", (aid, c["tg_id"]))
        if g and g["can_post"] and not g["ads_flag"]:
            db.ex("INSERT OR IGNORE INTO group_tags(account_id,tg_id,tag) VALUES(?,?,'avto-tayyor')", (aid, c["tg_id"]))
            db.ex("UPDATE disc_candidates SET status='active', reason='Reklama yuborishga tayyor' WHERE id=?", (c["id"],))
        else:
            db.ex("UPDATE disc_candidates SET status='noads', reason=? WHERE id=?",
                  ("Reklama taqiqlangan" if g and g["ads_flag"] else "Yozish mumkin emas", c["id"]))
        await asyncio.sleep(1.5)


# ---------------- kunlik sikl ----------------
async def tick():
    for a in db.q("SELECT a.* FROM accounts a JOIN autojoin j ON j.account_id=a.id WHERE j.active=1 AND a.workspace='posting'"):
        aid = a["id"]
        svc = manager.services.get(aid)
        if not svc or not svc.info:
            continue
        sync(aid)
        await activate_ready(aid)
        if work_gate(aid):
            continue
        cfg = get_cfg(aid)
        ready = db.one("SELECT COUNT(*) c FROM disc_candidates WHERE account_id=? AND status IN ('ready','approved')", (aid,))["c"]
        last = cfg["last_search"]
        stale = not last or datetime.strptime(last, "%Y-%m-%d %H:%M:%S") < datetime.now() - timedelta(hours=6)
        if ready < cfg["daily_target"] * 2 and stale and not progress.get(aid, {}).get("running"):
            await search(aid)
        plan_join(aid)


async def loop():
    await asyncio.sleep(240)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("discovery.tick")
        await asyncio.sleep(1200)
