## سند نیازمندی‌ها و مشخصات نسخهٔ تولیدی سامانهٔ تشخیص و ثبت پلاک پارکینگ (ورود / خروج)

**سامانهٔ مکمل:** این سند برای **سامانهٔ پارکینگ** است و از نظر **معماری، لایه‌ها و الگوی استقرار** با [سامانهٔ حضور و تردد مبتنی بر چهره](production_requirements_fa.md) هم‌راستا است؛ تفاوت اصلی در **دامنهٔ ساده‌تر** (پلاک به‌جای چهره، ورود و خروج به‌جای فقط ورود ساختمان) و **حذف ChromaDB / DeepFace** است.

**مستندات مرتبط (معماری مشترک):** [معماری محصول — حضور](product_architecture_pm.md) · [RBAC](rbac_roles.md) · [نیازمندی‌های تولید — حضور](production_requirements_fa.md)

**وضعیت پیاده‌سازی MVP (مخزن `parking`):** در انتهای بندهای قابل‌پیاده‌سازی: `✓` = انجام‌شده · `⬜` = انجام‌نشده. (علامت ✅ ابتدای بند = محدودهٔ تولیدی در سند مرجع `ch` — بدون تغییر.)

---a

### ۱. معرفی سامانه

- **شرح کلی سامانه**:  
  این سامانه یک سرویس تحت وب برای **تشخیص پلاک خودرو (ANPR)** و **ثبت رویداد ورود و خروج پارکینگ** است. هستهٔ سامانه بر پایهٔ **Flask**، **JWT**، **PostgreSQL** و **کارگر دوربین سرور** (`workers/camera_worker.py`) بنا می‌شود. تشخیص پلاک از فریم‌های دوربین ورودی/خروجی انجام می‌شود؛ مرورگر فقط برای **مانیتور زنده**، **ثبت خودرو**، **گزارش‌گیری** و **مدیریت پرسنل** استفاده می‌شود.

- **تفاوت با سامانهٔ حضور (ساده‌سازی)**:
  - بدون **ChromaDB** و **embedding** — تطبیق بر اساس **متن پلاک نرمال‌شده** در PostgreSQL است. ✓
  - بدون **DeepFace** — pipeline تشخیص: **YOLO / Ultralytics** (یا مدل معادل) برای ناحیهٔ پلاک + **OCR** برای خواندن کاراکترها. ✓
  - بدون **ردیابی ناشناس مبتنی بر وکتور** (ChromaDB) — تطبیق مهمان و ساکن هر دو بر اساس **پلاک ثبت‌شده** در PostgreSQL است. ✓
  - ✅ **خودروی مهمان** (همراه ساکن): ادمین (`parking_admin` / `worker`) می‌تواند خودروی مهمان با پلاک، تاریخ انقضا و اطلاعات مالک ثبت کند — همان الگوی `person_type=guest` در سامانهٔ حضور. ✓
  - ✅ **پلاک ناشناس** (ثبت‌نشده در DB): OCR موفق اما بدون match → لاگ `unregistered` با snapshot و cooldown متنی (بدون embedding). ✓
  - دو رویداد کسب‌وکاری: **ورود** (`entry`) و **خروج** (`exit`) — قابل پیکربندی با یک یا دو دوربین (`CAMERA_URL_ENTRY` / `CAMERA_URL_EXIT` یا `GATE_DIRECTION`). ⬜

- **هدف نسخهٔ تولیدی (Production)**:  
  نسخهٔ تولیدی باید:
  - در محیط واقعی پارکینگ (ورودی/خروجی) **پایدار، ایمن و قابل بهره‌برداری** باشد،
  - **ورود و خروج خودروها** را با پلاک خوانده‌شده (یا «ناشناس») **لاگ کند**،
  - امکان **مدیریت خودروهای مجاز (ساکن و مهمان)** و **پرسنل** را به شکل امن فراهم کند،
  - و **لاگ‌گیری، مانیتورینگ و خطایابی** مناسب برای عملیات روزانه داشته باشد.

---

### ۲. نیازمندی‌های عملکردی (Functional Requirements)

راهنما: مواردی که **در محدودهٔ نسخهٔ تولیدی** و **مطابق معماری مشترک** با پروژهٔ حضور هستند با ✅ مشخص شده‌اند. مواردی که برای استقرار تک‌سرور / یک پارکینگ **غیرضروری** تلقی شده‌اند با **⏭ غیرضروری** مشخص شده‌اند.

#### ۲.۱ احراز هویت و دسترسی
- ✅ **ورود پرسنل با JWT** (همان الگوی `auth_routes`): ✓
  - ✅ API لاگین `/auth/login` با `username` و `password`؛ برگرداندن **Access Token** و **Refresh Token**. ✓
  - ✅ طول عمر توکن‌ها از متغیرهای محیطی (پیش‌فرض پیشنهادی: ۱۵ دقیقه Access، ۷ روز Refresh). ✓
  - ✅ ذخیرهٔ JTI توکن Refresh در پایگاه داده و **Refresh Token Rotation**. ✓

- ✅ **نوسازی توکن**: ✓
  - ✅ API `/auth/refresh` فقط با Refresh Token معتبر؛ توکن چرخش‌یافته → **401**. ✓

- ✅ **محافظت از APIها**: ✓
  - ✅ middleware سراسری JWT برای همهٔ مسیرها به‌جز `/login`، `/auth/login`، `/auth/refresh`، `/health` و استاتیک. ✓
  - ✅ نقش‌ها مطابق [rbac_roles.md](rbac_roles.md): `system_admin`، `parking_admin` (معادل `building_admin`)، `worker`. ✓

#### ۲.۲ ثبت خودرو (Enrollment — جایگزین ثبت چهره)
- ✅ **ثبت پلاک مجاز — ساکن یا مهمان** (`POST /api/enroll` یا معادل): ✓
  - ✅ ورودی‌ها: `plate_number` (الزامی)، `owner_name`، `owner_lastname`، `parking_spot` / `unit` (اختیاری)، `vehicle_type` (اختیاری)، `notes` (اختیاری). ✓
  - ✅ **`vehicle_kind`** (یا `person_type`): `resident` | `guest` — پیش‌فرض `resident`؛ در UI ثبت (`/submit`) انتخاب رادیویی ساکن / مهمان (مشابه حضور). ✓
  - ✅ **مهمان**: فیلدهای `is_guest=true`، `guest_expires_at` (الزامی برای مهمان) — پس از انقضا خودرو از لیست مجاز حذف نرم یا purge خودکار (job پس‌زمینه، الگوی `helpers/guest_expiry.py`). ✓
  - ✅ **ساکن**: `is_guest=false`، `guest_expires_at=null` — ثبت دائمی تا soft delete توسط ادمین. ✓
  - ✅ `parking_admin` و `worker` هر دو می‌توانند ساکن و مهمان ثبت کنند (مطابق RBAC). ✓
  - ✅ **ستون‌های اجباری/توصیه‌شدهٔ جدول `vehicles`** (مقصد خودرو در ساختمان / پارکینگ): ✓
    - ✅ `car_model` — مدل خودرو (مثلاً پژو ۲۰۶، پراید). ✓
    - ✅ `door_number` — شمارهٔ درب / واحد مقصد. ✓
    - ✅ `floor_number` — طبقهٔ مقصد (خودرو «کجا می‌رود»). ✓
    - ✅ `metadata` — ستون **JSON** (PostgreSQL `JSONB`) برای فیلدهای اضافی بدون migration (پارکینگ اختصاصی، برچسب داخلی، …). ✓
  - ✅ **ثبت با تصویر**: آپلود یک یا چند تصویر مرجع (خودرو / پلاک) هنگام enroll؛ مسیر فایل در `reference_image_path` یا آرایه در `metadata.images`. ⬜
  - ✅ **`plate_color`** روی `vehicles`: رنگ پلاک ثبت‌شده (مثلاً `white`، `yellow`، `green`، `diplomatic`، …) برای تنظیم OCR/فیلتر؛ مقدار **`default`** برای پلاک‌های عادی سفید/معمولی ایران. ✓
  - ✅ **نرمال‌سازی پلاک** قبل از ذخیره (حذف فاصله، یکسان‌سازی حروف فارسی/لاتین، حروف بزرگ) — تابع مشترک `normalize_plate()`. ✓
  - ✅ قبل از ایجاد رکورد جدید، جستجو در PostgreSQL؛ اگر پلاک تکراری باشد → پاسخ «پلاک قبلاً ثبت شده» با `vehicle_id` موجود. ✓
  - ✅ ذخیرهٔ **تصویر مرجع** (اختیاری) در پوشهٔ `collection/` برای ممیزی؛ **بدون** ذخیرهٔ وکتور. ⬜
  - ✅ پاسخ شامل `status`، `vehicle_id`، `plate_number_normalized`، `duplicate: true|false`. ✓
 ⬜
- ✅ **نوع وسیله (اختیاری)**: ✓
  - ✅ فیلد `vehicle_class`: `car` | `motorcycle` | `other` روی `vehicles` — فقط برای گزارش/UI؛ **بدون** تأثیر روی pipeline تشخیص. ✓

#### ۲.۳ تشخیص پلاک و ثبت ورود/خروج (جایگزین احراز چهره)
- ✅ **کارگر دوربین سرور** (`workers/camera_worker.py`): ✓
  - ✅ خواندن فریم از **پیکربندی DB** (`settings` / `cameras`) — env فقط fallback برای bootstrap اولیه. ⬜
  - ✅ پشتیبانی از پروتکل‌های منبع تصویر per-camera: **`rtsp`**، **`http`** (MJPEG/اسنپ‌شات HTTP)، **`usb`** (ایندکس دستگاه V4L2). ✓
  - ✅ اجرای pipeline در بازهٔ `CAMERA_FRAME_INTERVAL_SECONDS` (پیش‌فرض ۱ ثانیه). ✓
  - ✅ **اسکن کل فریم**: YOLO/OCR روی **تمام تصویر** (بدون crop اولویت‌دار جلو/عقب یا منطق وابسته به `vehicle_class`)؛ هر ناحیهٔ پلاکی که در فریم دیده شود پردازش می‌شود. ⬜
  - ✅ برای هر فریم با پلاک قابل‌خواندن: ⬜
    - ✅ استخراج باکس پلاک و متن OCR؛ **confidence** حداقل از env (`PLATE_OCR_MIN_CONFIDENCE`). ✓
    - ✅ جستجوی پلاک نرمال‌شده در PostgreSQL. ✓
    - ✅ وضعیت: **registered** (ساکن یا مهمان فعال در `vehicles`) / **unregistered** (پلاک خوانده شد اما ثبت نشده) / **unreadable** (پلاک دیده نشد یا OCR ناموفق — معمولاً بدون لاگ کسب‌وکاری). ✓
    - ✅ اگر `guest_expires_at` گذشته باشد → match نمی‌شود (مثل پلاک ثبت‌نشده)؛ purge دوره‌ای رکورد مهمان منقضی. ✓

- ✅ **جهت رویداد (ورود / خروج)**: ✓
  - ✅ هر لاگ شامل `direction`: `entry` | `exit` (از تنظیم دوربین یا پارامتر worker). ✓
  - ✅ در صورت **یک دوربین** برای هر دو جهت: جهت از env ثابت (`GATE_DIRECTION=entry|exit`) یا از پیکربندی دو worker جدا. ✓

- ✅ **ثبت لاگ پارکینگ** (`parking_logs` — معادل `entrance_logs`): ✓
  - ✅ پس از تشخیص موفق، ردیف در `parking_logs` با: `plate_number`، `plate_normalized`، `direction`، `match_status` (`registered` | `unregistered`)، `is_guest` (در صورت match مهمان)، `vehicle_id`، `snapshot_path`، `confidence`، `logged_at`. ✓
  - ✅ **Cooldown** (`PARKING_LOG_COOLDOWN_SECONDS`، پیش‌فرض ۶۰۰): عدم تکرار لاگ برای همان `plate_normalized` + `direction` در بازهٔ cooldown. ✓
  - ✅ کلید حافظه: `registered:<vehicle_id>:<direction>` یا `unregistered:<plate_normalized>:<direction>`. ✓

- ✅ **پشتیبانی چند پلاک در یک فریم** (اختیاری ولی توصیه‌شده): ✓
  - ✅ لیست نتایج per-plate در payload داخلی؛ برای هر پلاک جداگانه cooldown و لاگ. ✓

- ✅ **استریم زنده برای نگهبان** *(اختیاری در UI — پیش‌فرض خاموش)*: ✓
  - ✅ `GET /api/live/stream` (MJPEG) با کادر روی ناحیهٔ پلاک: **سبز** = ساکن ثبت‌شده، **زرد** = مهمان ثبت‌شده، **قرمز** = پلاک خوانده‌شده اما ثبت‌نشده / OCR ضعیف. ✓
  - ✅ تشخیص و لاگ در **پس‌زمینه** حتی وقتی هیچ کلاینتی استریم را باز نکرده است. ✓

- ✅ **مدیریت نور / بازتاب** (مثلاً تابش خورشید روی خودرو): ⬜
  - ✅ تنظیمات per-camera یا global در `settings`: `light_profile` (`normal` | `high_glare` | `low_light`)، آستانهٔ OCR جدا، پیش‌پردازش (CLAHE، crop ROI). ⬜
  - ✅ `parking_admin` / `system_admin` می‌تواند از پنل یا API پروفایل نور را عوض کند بدون redeploy. ⬜
  - ✅ در صورت glare مکرر → `software_logs` با `event=lighting_warning` (بدون crash worker). ⬜

#### ۲.۳.۱ پایگاه تنظیمات و دوربین‌ها (`settings` / `cameras`)
- ✅ **جدول `cameras`** (مدیریت توسط `parking_admin` و `system_admin` از پنل ادمین): ⬜
  - ✅ `name`، `protocol` (`rtsp` | `http` | `usb`)، `source` (URL یا شمارهٔ USB). ⬜
  - ✅ `gate_role`: `entry` | `exit` — مشخص می‌کند لاگ‌های این دوربین با `direction` ورود یا خروج ثبت شوند. ⬜
  - ✅ `is_enabled`، `frame_interval_seconds` (اختیاری override)، `light_profile`. ⬜
  - ✅ worker در startup لیست دوربین‌های فعال را از DB می‌خواند؛ تغییر از API → reload محدود یا restart worker با سیگنال امن. ⬜
- ✅ **جدول `settings`** (کلید–مقدار یا JSONB): ⬜
  - ✅ آستانه‌های پیش‌فرض OCR، cooldown، مسیر snapshot، GPU on/off، پروفایل نور global. ⬜
  - ✅ env (`CAMERA_URL_ENTRY`، …) فقط برای نصب اولیه؛ پس از migrate به DB، منبع حقیقت = PostgreSQL. ⬜

#### ۲.۴ مدیریت داده‌ها و ریست
- ✅ **ریست سامانه** (`GET /reset`، فقط `system_admin`): ✓
  - ✅ خالی‌کردن جداول `vehicles`، `parking_logs`، `software_logs` (طبق سیاست پروژه)، ✓
  - ✅ حذف فایل‌های `collection/` و `uploads/` (شامل `unknown_parking_logs/`), ✓
  - ✅ بدون ChromaDB — نیازی به reset وکتور نیست. ✓

- ✅ **مدیریت و گزارش**:
  - ✅ `GET /api/vehicles` — لیست خودروهای مجاز با فیلتر پلاک / نام / `is_guest` (ساکن / مهمان). ✓
  - ✅ `GET /api/parking-logs` — تاریخچهٔ ورود/خروج با فیلتر تاریخ، جهت، وضعیت match. ✓
  - ✅ `GET /api/parking-snapshot` — دسترسی امن به تصویر رویداد (JWT). ✓
  - ✅ `GET /api/software-logs` + پنل ادمین. ⬜
  - ✅ `GET/POST/PATCH/DELETE /api/admins` — مدیریت پرسنل (نقش‌ها مطابق RBAC). ⬜
  - ✅ `POST /api/remove-vehicle` — **حذف نرم (soft delete)** خودرو با `vehicle_id` یا پلاک؛ **هیچ `DELETE` فیزیکی از DB** برای رکوردهای کسب‌وکاری. ✓

- ✅ **حذف نرم (Soft delete)** — الزام تولید: ✓
  - ✅ ستون `deleted_at` (nullable timestamp) روی `vehicles`، `parking_logs` (در صورت نیاز حذف لاگ از UI)، و در صورت تمایل `cameras`. ✓
  - ✅ API حذف فقط `deleted_at = now()` می‌گذارد؛ داده برای ممیزی و گزارش حقوقی باقی می‌ماند. ✓
  - ✅ **همهٔ queryهای UI و API گزارش** فیلتر `WHERE deleted_at IS NULL`. ✓
  - ✅ `parking_logs` مرتبط با خودروی soft-deleted در UI لاگ **نمایش داده نمی‌شوند** (یا با فلگ admin «شامل حذف‌شده» — پیش‌فرض: مخفی). ⬜

#### ۲.۵ رابط کاربری وب
- ✅ **صفحهٔ مانیتور (`/`)** — **لیست خودروها، نه دوربین زنده**: ✓
  - ✅ نمایش **ستون عمودی** از آخرین خودروهای ثبت‌شده در لاگ (پلاک، جهت، زمان، وضعیت match، تصویر بندانگشتی snapshot). ✓
  - ✅ **بدون استریم زنده به‌صورت پیش‌فرض** — کاهش بار مرورگر و تمرکز نگهبان روی رویدادها. ✓
  - ✅ دکمه/سوییچ **«فعال‌سازی دوربین زنده»** → بارگذاری اختیاری `GET /api/live/stream` (MJPEG). ✓
  - ✅ تشخیص در سرور همیشه فعال است؛ UI فقط نمایش زنده را کنترل می‌کند. ✓

- ✅ **صفحهٔ ثبت خودرو (`/submit`)**: ✓
  - ✅ فرم پلاک + اطلاعات مالک؛ انتخاب **ساکن / مهمان**؛ برای مهمان: تاریخ انقضا (`guest_expires_at`). ✓
  - ✅ آپلود تصویر اختیاری یا عکس از دوربین ثابت ثبت‌نام (در صورت وجود). ⬜

- ✅ **صفحهٔ حذف / جستجو (`/remove` یا `/vehicles`)**: ⬜
  - ✅ جستجو و حذف خودروی مجاز. ⬜

- ✅ **پنل ادمین (`/admin`)**: ⬜
  - ✅ خودروهای مجاز، تاریخچهٔ ورود/خروج، لاگ نرم‌افزاری، مدیریت حساب‌ها. ⬜
  - ✅ **پیکربندی دوربین‌ها** (ورود/خروج، پروتکل، URL/USB، فعال/غیرفعال) — فقط نقش‌های `parking_admin` و `system_admin`. ⬜
  - ✅ تنظیم پروفایل نور و آستانهٔ OCR (در حد مجاز RBAC). ⬜

- ⏭ **غیرضروری — بومی‌سازی کامل فارسی UI** (راست‌چین در صورت نیاز قرارداد جدا).

#### ۲.۶ قابلیت‌های اضافهٔ پیشنهادی *(خارج از حداقل، هم‌سطح ساده‌تر از حضور)*

- ✅ `GET /api/live/status` — وضعیت دوربین(ها) و آخرین تشخیص. ✓
- ✅ `GET /health` — سلامت DB + دوربین. ✓
- ⏭ **غیرضروری**: یکپارچه‌سازی با راهبند فیزیکی (GPIO / relay) — فاز بعد.
- ⏭ **غیرضروری**: اعلان بلادرنگ (SMS / Telegram) برای پلاک ثبت‌نشده.

---

### ۳. نیازمندی‌های غیرعملکردی (Non-Functional Requirements)

#### ۳.۱ کارایی و مقیاس‌پذیری
- ✅ **تاخیر هدف** *(ساده‌تر از چهره — بدون DeepFace)*: ⬜
  - ✅ از فریم تا ثبت لاگ برای **یک پلاک** در بار عادی: هدف **کمتر از ۰.۵–۱ ثانیه** روی CPU مناسب؛ با **GPU** هدف **کمتر از ۰.۳ ثانیه**. ⬜
  - ✅ OCR + YOLO: در تولید **استفاده از GPU توصیه‌شده / الزام قراردادی** (`PLATE_USE_GPU=true` یا تشخیص خودکار CUDA) برای چند دوربین RTSP و فاصلهٔ فریم کم. ⬜
  - ✅ Inference روی GPU (PyTorch/CUDA یا ONNX Runtime GPU)؛ fallback CPU فقط برای dev یا سخت‌افزار بدون GPU. ⬜

- ⏭ **غیرضروری — هم‌زمانی ۵۰+ وب‌سوکت** (معماری فعلی: یک pipeline + چند بیننده MJPEG کافی است).

- ⏭ **غیرضروری — مقیاس افقی + Redis برای Socket.IO** (تک‌سرور per gate کافی است).

#### ۳.۲ دسترس‌پذیری و پایداری
- ✅ **اجرای دائمی**: Docker + Docker Compose؛ `GET /health`. ✓
- ✅ **تحمل خطا**:
  - ✅ خطای OCR / مدل → لاگ نرم‌افزاری + ادامهٔ loop دوربین؛ بدون crash فرآیند. ✓
  - ✅ Retry محدود روی inference (`PLATE_DETECT_MAX_RETRIES` از env). ⬜
  - ✅ قطع موقت RTSP → reconnect در worker (همان الگوی حضور). ✓

- ⏭ **غیرضروری**: Kubernetes.

#### ۳.۳ امنیت
- ✅ اسرار از **متغیرهای محیطی** (`JWT_SECRET_KEY`، `DATABASE_URL`، …). ✓
- ✅ JWT روی APIهای مدیریتی؛ `/reset` فقط `system_admin`. ✓
- ✅ `JWT_COOKIE_SECURE` پشت HTTPS؛ `FLASK_DEBUG=false` در production. ✓
- ⏭ **غیرضروری**: رمزنگاری دیسک / پنتست رسمی (سیاست مشتری).

#### ۳.۴ لاگ‌گیری و مانیتورینگ
- ✅ **software_logs** (PostgreSQL): لاگین، خطای دوربین، شکست OCR، reset، soft delete خودرو، هشدار نور. ✓
- ✅ **parking_logs**: رویداد کسب‌وکاری «چه پلاکی چه زمانی ورود/خروج کرد». ✓
- ✅ **لاگ استثنا و crash در ترمینال** (همراه DB):
  - ✅ `logging` استاندارد Python به **stdout/stderr** با سطح `ERROR`/`CRITICAL` برای traceback کامل. ✓
  - ✅ `try/except` در worker و pipeline: ثبت در `software_logs` **و** چاپ در ترمینال (Docker logs) برای عیب‌یابی سریع ops. ✓
  - ✅ handler سراسری برای exceptionهای Flask (۵۰۰) با پیام یک‌خطی در ترمینال + ردیف `software_logs`. ✓
- ⏭ **غیرضروری**: Prometheus / Grafana / لاگ JSON استاندارد.

---

### ۴. نیازمندی‌های زیرساخت و استقرار

#### ۴.۱ پیکربندی محیط
- ✅ متغیرهای پیشنهادی (هم‌ساخت با `.env.example` حضور، با تفاوت‌های دامنه): ✓

| متغیر | کاربرد | MVP |
|--------|--------|-----|
| `JWT_SECRET_KEY` | امضای JWT | ✓ |
| `DATABASE_URL` | PostgreSQL | ✓ |
| `CAMERA_URL` یا `CAMERA_URL_ENTRY` / `CAMERA_URL_EXIT` | bootstrap اولیه؛ پس از راه‌اندازی → جدول `cameras` | ✓ |
| `GATE_DIRECTION` | fallback وقتی یک دوربین و DB خالی | ✓ |
| `PLATE_USE_GPU` | `true`/`false` — inference روی GPU | ⬜ |
| `CAMERA_FRAME_INTERVAL_SECONDS` | فاصلهٔ تشخیص | ✓ |
| `PARKING_LOG_COOLDOWN_SECONDS` | جلوگیری از لاگ تکراری | ✓ |
| `GUEST_RETENTION_DAYS` | purge خودکار خودروی مهمان پس از `guest_expires_at` (پیش‌فرض ۳۰) | ✓ |
| `PLATE_OCR_MIN_CONFIDENCE` | آستانهٔ قبول OCR | ✓ |
| `UPLOAD_FOLDER` / `COLLECTION_FOLDER` | تصاویر موقت و مرجع | ✓ |
| `ENV` | `development` \| `production` | ✓ |

- ⏭ **غیرضروری**: سه محیط جدا Development / Staging / Production در infra مشتری.

#### ۴.۲ پایگاه داده و ذخیره‌سازی
- ✅ **PostgreSQL** (تولید) — جداول پیشنهادی: ✓

| جدول | معنی کسب‌وکاری | MVP |
|------|----------------|-----|
| `vehicles` | خودروهای مجاز: `plate_*`، `is_guest`، `guest_expires_at`، `car_model`، `door_number`، `floor_number`، `plate_color` (پیش‌فرض `default`)، `vehicle_class`، `metadata` (JSONB)، `reference_image_path`، `deleted_at` | ✓ |
| `cameras` | دوربین‌ها: `protocol`، `source`، `gate_role` (entry/exit)، `light_profile`، `is_enabled` | ⬜ |
| `settings` | تنظیمات سراسری (کلید–مقدار / JSONB): OCR، cooldown، GPU، نور | ⬜ |
| `admins` | پرسنل و نقش | ✓ |
| `parking_logs` | ورود/خروج؛ `deleted_at` برای مخفی‌سازی از UI | ✓ |
| `software_logs` | ممیزی فنی + هم‌راستا با لاگ ترمینال | ✓ |

- ✅ **بدون ChromaDB** — کاهش پیچیدگی backup و استقرار. ✓
- ✅ Volume دائمی برای `uploads/` (شامل `unknown_parking_logs/`) و `collection/`. ✓

#### ۴.۳ کانتینرسازی
- ✅ `Dockerfile` + `docker-compose.yml`: سرویس `app` + `postgres`. ✓
- ✅ Mount Volumeها؛ پورت قابل تنظیم (پیش‌فرض ۵۰۰۰). ✓
- ✅ variant **GPU (CUDA)** برای تولید؛ image CPU-only فقط dev/بدون کارت گرافیک. ⬜
- ⏭ **غیرضروری**: Redis، Kubernetes.

---

### ۵. نیازمندی‌های کیفی و تست

#### ۵.۱ تست عملکردی
- ✅ لاگین JWT (موفق / ناموفق) و refresh rotation. ⬜
- ✅ `POST /api/enroll`: ✓
  - ✅ پلاک جدید (ساکن)، ✓
  - ✅ پلاک مهمان با `guest_expires_at`، ✓
  - ✅ مهمان بدون تاریخ انقضا → خطای اعتبارسنجی، ✓
  - ✅ پلاک تکراری، ✓
  - ✅ پلاک با فاصله/حروف مختلف (نرمال‌سازی یکسان). ✓
  - ✅ purge مهمان منقضی (job پس‌زمینه). ✓
- ✅ کارگر دوربین / pipeline: ✓
  - ✅ پلاک ساکن → `registered` + `is_guest=false`, ✓
  - ✅ پلاک مهمان فعال → `registered` + `is_guest=true`, ✓
  - ✅ پلاک مهمان منقضی → `unregistered` (یا بدون match), ✓
  - ✅ پلاک خوانده‌شده اما غیرمجاز → `unregistered`, ✓
  - ✅ فریم بدون پلاک → بدون لاگ کسب‌وکاری (یا فقط software_log در صورت خطای مکرر). ✓
- ✅ cooldown: همان پلاک در ۱۰ دقیقه دو بار لاگ نشود (مگر جهت متفاوت entry vs exit). ✓
- ✅ `/reset` — پاکسازی DB و فایل‌ها. ✓

#### ۵.۲ تست کارایی
- ⏭ **غیرضروری**: load test رسمی ۵۰+ کلاینت.

#### ۵.۳ تست امنیت
- ✅ JWT منقضی / دست‌کاری‌شده → 401. ⬜
- ✅ مسیرهای محافظت‌شده بدون توکن → 401/403. ⬜
- ⏭ **غیرضروری**: ممیزی امنیتی شخص ثالث.

---

### ۶. الزامات عملیاتی و نگه‌داری

- ✅ مستند API (`API_DOCUMENTATION.md` — نسخهٔ پارکینگ). ⬜
- ✅ راهنمای استقرار و worker دوربین (مشابه `camera_worker.md`). ⬜
- ⏭ **غیرضروری**: runbook backup قراردادی (مسئولیت ops).
- ✅ به‌روزرسانی مدل YOLO/OCR و آستانه‌ها از env بدون تغییر کد هسته. ✓

---

### ۶.۱ نمونه وضعیت حافظه برای cooldown پلاک (ساده‌تر از ردیابی چهره)

برای جلوگیری از spam لاگ، فقط یک map زمانی کافی است (بدون embedding ناشناس):

```python
_last_parking_log_at = {
    "registered:42:entry": datetime.datetime(2026, 5, 23, 10, 15, 0, tzinfo=datetime.timezone.utc),
    "unregistered:12ب34567:exit": datetime.datetime(2026, 5, 23, 10, 20, 5, tzinfo=datetime.timezone.utc),
}
```

- ✅ TTL اختیاری برای پاکسازی کلیدهای قدیمی در اجرای طولانی‌مدت (`PARKING_COOLDOWN_MAP_TTL_SECONDS`). ⬜
- ⏭ **غیرضروری**: ردیابی چندفریمی پلاک ناشناس با وکتور (دامنهٔ حضور — در پارکینگ کافی نیست).

---

### ۶.۲ معماری نرم‌افزار (هم‌راستا با سامانهٔ حضور)

```
┌─────────────┐     فریم زنده      ┌──────────────────────────────────────┐
│  دوربین(ها) │ ────────────────► │  python main.py (فرآیند واحد)         │
│  ورود/خروج  │                   │  ├── Flask (HTTP + JWT + صفحات)       │
└─────────────┘                   │  ├── camera_worker (capture + queue)  │
                                  │  ├── plate_pipeline (YOLO + OCR)      │
                                  │  └── parking_logging (DB + snapshots) │
                                  └──────────────┬───────────────────────┘
                                                 │
                    ┌────────────────────────────┼────────────────────────────┐
                    ▼                            ▼                            ▼
             ┌─────────────┐            ┌─────────────┐              ┌─────────────┐
             │ PostgreSQL  │            │  uploads/   │              │ collection/ │
             │ vehicles    │            │  snapshots  │              │  (اختیاری)  │
             │ parking_logs│            └─────────────┘              └─────────────┘
             │ software_logs│
             └─────────────┘
```

**ساختار پیشنهادی مخزن** (آینهٔ `product_architecture_pm.md`):

```
parking-plates/
├── main.py ✓
├── workers/camera_worker.py ✓
├── routes/          # app_routes, auth_routes, plate_detect, enroll, remove ✓
├── helpers/ ✓
│   ├── plate_pipeline.py ✓
│   ├── parking_logging.py ✓
│   ├── live_frame_buffer.py ✓
│   └── plate_normalize.py ✓
├── database/        # models, vehicles_db, logs_db, admin_db ✓
├── templates/       # ui, login, submit, admin ✓
└── documents/       # این سند + API + camera_worker ✓
``` ✓

**اصل طراحی (مشترک با حضور):** منبع حقیقت امنیتی = **دوربین سرور**؛ مرورگر فقط مانیتور و مدیریت.

---

### ۷. جمع‌بندی برای قرارداد کاری

- این سند **نیازمندی‌ها و مشخصات نسخهٔ تولیدی** سامانهٔ **تشخیص و ثبت پلاک پارکینگ (ورود/خروج)** را توصیف می‌کند و قابل استفاده به‌عنوان **پیوست فنی** در قرارداد جدا از سامانهٔ حضور است.
- **دامنه (Scope)** نسبت به حضور:
  - ✅ حفظ شده: Flask، JWT، RBAC، کارگر دوربین، MJPEG زنده، PostgreSQL، لاگ دو‌لایه (کسب‌وکاری + نرم‌افزاری)، Docker، admin panel. ✓
  - ✅ حذف / ساده‌شده: ChromaDB، DeepFace، embedding، ردیابی ناشناس **وکتوری**، تشخیص چندچهره‌ای. ✓
  - ✅ حفظ شده (با تطبیق پلاک): **مهمان با انقضا**، purge خودکار پس از `guest_expires_at`. ✓
  - ✅ افزوده: جهت **ورود/خروج**، مدل دادهٔ **خودرو/پلاک**، OCR پلاک. ✓
  - ✅ افزوده (نسخهٔ به‌روز): **DB تنظیمات و دوربین**، **حذف نرم**، **UI لیست عمودی بدون زندهٔ پیش‌فرض**، **رنگ پلاک**، **مدیریت نور**، **لاگ exception در ترمینال**، **GPU در تولید**. ⬜
- بر اساس این سند می‌توان **معیار پذیرش (Acceptance Criteria)** و سناریوهای تست را برای استقرار در **یک ورودی/خروجی پارکینگ** تعریف کرد.

---

### ۸. خلاصهٔ نیازمندی‌های تکمیلی (چک‌لیست سریع)

| موضوع | الزام | MVP |
|--------|--------|-----|
| نور / glare خورشید | پروفایل `light_profile` + پیش‌پردازش و آستانهٔ قابل تنظیم از settings | ⬜ |
| دوربین توسط ادمین | جدول `cameras`: entry/exit، `rtsp` / `http` / `usb` | ⬜ |
| UI مانیتور | لیست عمودی خودروهای لاگ‌شده؛ دوربین زنده فقط با opt-in | ✓ |
| حذف | soft delete (`deleted_at`)؛ بدون نمایش در UI لاگ | ✓ |
| Crash / exception | `software_logs` + traceback در ترمینال (Docker logs) | ✓ |
| کارایی | GPU برای inference در استقرار تولید | ⬜ |
| رنگ پلاک | `vehicles.plate_color`؛ مقدار پیش‌فرض `default` | ✓ |
| نوع وسیله (اختیاری) | `vehicle_class` روی `vehicles`؛ pipeline همیشه کل فریم را اسکن می‌کند | ⬜ |
| ثبت خودرو | تصویر + `car_model`، `door_number`، `floor_number`، `metadata` JSON | ✓ |
| مهمان | `is_guest` + `guest_expires_at`؛ ثبت توسط ادمین/worker؛ purge پس از انقضا | ✓ |

---

*وضعیت این سند:* مشخصات **سامانهٔ پارکینگ (مکمل حضور)** — پیاده‌سازی MVP در مخزن `parking` — وضعیت بندها با ✓ / ⬜ در همین سند؛ معماری از [production_requirements_fa.md](production_requirements_fa.md) و [product_architecture_pm.md](product_architecture_pm.md) اقتباس شده است. برای نقش‌ها: [rbac_roles.md](rbac_roles.md) (با جایگزینی `building_admin` → `parking_admin` در UI/قرارداد در صورت نیاز).
