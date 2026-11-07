import asyncio
import hashlib
import json
import os
import re
import smtplib
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_SECRET_FILE = os.path.join(os.path.expanduser("~"), ".gemini", "config", "colsubsidio_salud_secret.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config_salud.json")
STATE_FILE = os.path.join(BASE_DIR, "estado_cita.json")
REPORT_MD = os.path.join(BASE_DIR, "reporte_cita_colsubsidio.md")
CONFIRM_SCREENSHOT = os.path.join(BASE_DIR, "cita_confirmada.png")

BOGOTA_TZ = timezone(timedelta(hours=-5))


def now_bogota() -> datetime:
    return datetime.now(BOGOTA_TZ)


def mask_id(val: str) -> str:
    """Oculta números sensibles en logs y reportes públicos (ej. ***4681)."""
    s = str(val or "").strip()
    if len(s) <= 4:
        return "***"
    return f"***{s[-4:]}"


def format_12h(hora_raw: str) -> tuple[str, str]:
    hh_mm = (hora_raw or "08:00")[:5]
    try:
        hh_int, mm_int = [int(x) for x in hh_mm.split(":")]
        ampm = "pm" if hh_int >= 12 else "am"
        hh_12 = hh_int % 12 or 12
        return hh_mm, f"{hh_12}:{mm_int:02d} {ampm}"
    except Exception:
        return hh_mm, hh_mm


def normalize_text(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_str).strip().lower()


def matches_yuliet_cardenas(doctor_name: str) -> bool:
    """
    Verifica estrictamente si el nombre del profesional corresponde a la Dra. Yuliet Cárdenas
    (soportando variantes Yuliet/Yulieth/Juliet/Julieth/Julie y orden Apellidos-Nombres).
    """
    norm = normalize_text(doctor_name)
    if not norm or "medicodisponible" in norm.replace(" ", ""):
        return False
    has_first = any(v in norm for v in ("yuliet", "yulieth", "juliet", "julieth", "yulie", "julie"))
    has_last = "cardenas" in norm
    return has_first and has_last


def load_config() -> dict:
    cfg = {}
    for candidate in (LOCAL_SECRET_FILE, CONFIG_FILE):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception as e:
                print(f"[WARN] No se pudo leer configuración local ({os.path.basename(candidate)}): {e}")

    return {
        "doc_type_option": os.environ.get("COLSUBSIDIO_DOC_OPTION") or cfg.get("doc_type_option", "0: 2"),
        "doc_type_code": os.environ.get("COLSUBSIDIO_DOC_CODE") or cfg.get("doc_type_code", "CC"),
        "username": (os.environ.get("COLSUBSIDIO_USER") or cfg.get("colsubsidio_user", "")).strip(),
        "password": (os.environ.get("COLSUBSIDIO_PASS") or cfg.get("colsubsidio_pass", "")).strip(),
        "target_doctor": os.environ.get("TARGET_DOCTOR") or cfg.get("target_doctor", "Yuliet Cárdenas"),
        "auto_book": str(os.environ.get("AUTO_BOOK", cfg.get("auto_book", True))).lower() in ("1", "true", "yes", "si"),
        "windows_popup": str(os.environ.get("WINDOWS_POPUP", cfg.get("windows_popup", True))).lower() in ("1", "true", "yes", "si"),
        "smtp_enabled": bool(os.environ.get("SMTP_SENDER") and os.environ.get("SMTP_APP_PASSWORD")) or bool(cfg.get("smtp_enabled", True)),
        "smtp_server": os.environ.get("SMTP_SERVER") or cfg.get("smtp_server", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT") or cfg.get("smtp_port", 587)),
        "smtp_sender": (os.environ.get("SMTP_SENDER") or cfg.get("smtp_sender", "")).strip(),
        "smtp_password": (os.environ.get("SMTP_APP_PASSWORD") or cfg.get("smtp_app_password", "")).replace(" ", "").strip(),
        "smtp_recipient": (os.environ.get("SMTP_RECIPIENT") or cfg.get("smtp_recipient", "")).strip(),
    }


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"booked": False, "notified_other_hashes": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def write_report(status_title: str, details_lines: list[str]) -> str:
    ts = now_bogota().strftime("%Y-%m-%d %I:%M:%S %p (Hora Colombia)")
    md = [
        "# 🩺 Monitor Cita Medicina Familiar — Salud Colsubsidio",
        "",
        f"- **Última verificación:** {ts}",
        "- **Agendamiento automático exclusivo con:** Dra. Yuliet Cárdenas",
        "- **Otros médicos:** Solo aviso informativo (sin agendar)",
        f"- **Estado actual:** {status_title}",
        "",
        "## Detalle de la revisión",
    ]
    for line in details_lines:
        md.append(f"- {line}")
    content = "\n".join(md) + "\n"
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(content)
    return content


def trigger_windows_alert(title: str, message: str, urgent: bool = True) -> None:
    """Muestra alerta emergente con sonido en Windows sin bloquear el proceso principal."""
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") == "true":
        return
    try:
        safe_title = title.replace("'", "''").replace('"', "`\"")
        safe_msg = message.replace("'", "''").replace('"', "`\"")
        beep_seq = (
            "[console]::beep(880,300); [console]::beep(1175,450); "
            if urgent
            else "[console]::beep(660,250); "
        )
        ps_script = (
            "Add-Type -AssemblyName PresentationFramework,System.Windows.Forms; "
            "[System.Media.SystemSounds]::Exclamation.Play(); Start-Sleep -Milliseconds 350; "
            + beep_seq
            + f"[System.Windows.Forms.MessageBox]::Show('{safe_msg}', '{safe_title}', "
            "'OK', 'Information', 'Button1', 'ServiceNotification') | Out-Null"
        )
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=creation_flags,
            close_fds=True,
        )
    except Exception as e:
        print(f"[WARN] No se pudo mostrar alerta emergente en Windows: {e}")


def send_email_notification(cfg: dict, subject: str, html_body: str, screenshot_path: str | None = None) -> tuple[bool, str]:
    if not cfg.get("smtp_enabled"):
        return False, "Correo desactivado."
    sender = cfg.get("smtp_sender", "")
    password = cfg.get("smtp_password", "")
    raw_recipients = cfg.get("smtp_recipient", "") or sender
    recipients = [r.strip() for r in raw_recipients.split(",") if r.strip()]
    if not (sender and password and recipients):
        return False, "Faltan credenciales SMTP."

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        if screenshot_path and os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as img_f:
                part = MIMEBase("image", "png")
                part.set_payload(img_f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", 'attachment; filename="confirmacion_cita_colsubsidio.png"')
            msg.attach(part)

        try:
            with smtplib.SMTP_SSL(cfg["smtp_server"], 465, timeout=30) as server:
                server.ehlo()
                server.login(sender, password)
                server.sendmail(sender, recipients, msg.as_string())
        except Exception:
            with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"], timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(sender, password)
                server.sendmail(sender, recipients, msg.as_string())
        return True, f"Notificación enviada por correo ({len(recipients)} destinatario(s))."
    except Exception as e:
        return False, f"Error enviando correo: {e}"


async def launch_browser(p):
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    if sys.platform == "win32" and os.environ.get("GITHUB_ACTIONS") != "true":
        for ch in ("chrome", "msedge"):
            try:
                return await p.chromium.launch(channel=ch, headless=True, args=launch_args)
            except Exception:
                continue
    return await p.chromium.launch(headless=True, args=launch_args)


async def attempt_ui_booking(page, winning_slot: dict) -> tuple[bool, str]:
    """
    Agenda la cita en el formulario Angular (#/agendarcitag/...) verificando estrictamente
    que el especialista seleccionado sea la Dra. Yuliet Cárdenas.
    """
    try:
        city_name = winning_slot["city_name"]
        ips_name = winning_slot["ips_name"]
        jornada = winning_slot["jornada"]
        fecha_str = winning_slot["fecha"]
        hora_ap = winning_slot["hora_ap"]
        hora_24 = winning_slot["hora_24"]

        city_selects = page.locator("app-user-data-appointments ng-select")
        if await city_selects.count() > 0:
            await city_selects.first.click()
            await page.wait_for_timeout(800)
            opt_city = page.locator(".ng-option", has_text=re.compile(re.escape(city_name), re.I))
            if await opt_city.count() > 0:
                await opt_city.first.click()
            else:
                await page.locator(".ng-option").first.click()
            await page.wait_for_timeout(2500)

        por_centro = page.locator("text=/centro m[ée]dico/i")
        for i in range(await por_centro.count()):
            if await por_centro.nth(i).is_visible():
                txt_pc = (await por_centro.nth(i).inner_text()).strip()
                if len(txt_pc) < 40:
                    try:
                        await por_centro.nth(i).click(timeout=2000)
                        await page.wait_for_timeout(1200)
                        break
                    except Exception:
                        pass

        mc_input = page.locator("#medical-center")
        if await mc_input.count() > 0 and await mc_input.first.is_visible():
            mc_ng = page.locator("app-medical-center ng-select").first
            await mc_ng.click()
            await page.wait_for_timeout(800)
            opt_ips = page.locator(".ng-option", has_text=re.compile(re.escape(ips_name), re.I))
            if await opt_ips.count() > 0:
                await opt_ips.first.click()
                await page.wait_for_timeout(2000)

        if jornada in ("M", "T"):
            jornada_regex = r"ma[ñn]ana" if jornada == "M" else r"tarde"
            j_loc = page.locator(f"label:has-text(/{jornada_regex}/i), input[value='{jornada}']")
            for i in range(await j_loc.count()):
                if await j_loc.nth(i).is_visible():
                    await j_loc.nth(i).click()
                    await page.wait_for_timeout(2500)
                    break

        y, m, d = [int(x) for x in fecha_str.split("-")]
        for _ in range(6):
            clicked_day = await page.evaluate(
                """({day}) => {
                    const cells = Array.from(document.querySelectorAll('.ngb-dp-day:not(.disabled):not(.hidden)'));
                    for (const cell of cells) {
                        const txt = (cell.innerText || '').trim();
                        if (txt === String(day) && !cell.classList.contains('text-muted')) {
                            cell.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                {"day": d},
            )
            if clicked_day:
                break
            next_arrow = page.locator("button.ngb-dp-arrow-btn").last
            if await next_arrow.count() > 0 and await next_arrow.is_visible():
                await next_arrow.click()
                await page.wait_for_timeout(800)
        await page.wait_for_timeout(2500)

        hour_ng = page.locator("ng-select:has(#cita-hour)")
        if await hour_ng.count() > 0:
            await hour_ng.first.click()
            await page.wait_for_timeout(800)
            opt_hr = page.locator(".ng-option", has_text=re.compile(re.escape(hora_ap), re.I))
            if await opt_hr.count() == 0:
                opt_hr = page.locator(".ng-option", has_text=re.compile(re.escape(hora_24), re.I))
            if await opt_hr.count() > 0:
                await opt_hr.first.click()
                await page.wait_for_timeout(1500)

        # VERIFICACIÓN ESTRICTA: SOLO SELECCIONAR SI ES LA DRA. YULIET CÁRDENAS
        spec_ng = page.locator("ng-select:has(#select-specialist)")
        if await spec_ng.count() > 0:
            await spec_ng.first.click()
            await page.wait_for_timeout(800)
            options_count = await page.locator(".ng-option").count()
            matched_opt_idx = -1
            for idx in range(options_count):
                opt_txt = (await page.locator(".ng-option").nth(idx).inner_text()).strip()
                if matches_yuliet_cardenas(opt_txt):
                    matched_opt_idx = idx
                    break
            if matched_opt_idx < 0:
                return False, "No apareció la Dra. Yuliet Cárdenas en el desplegable visual."
            await page.locator(".ng-option").nth(matched_opt_idx).click()
            await page.wait_for_timeout(1500)

        chk = page.locator("mat-icon:has-text('check_box_outline_blank')")
        if await chk.count() > 0 and await chk.first.is_visible():
            await chk.first.click()
            await page.wait_for_timeout(800)

        step1_btn = page.locator("#nextagenda_step1")
        if await step1_btn.count() > 0 and await step1_btn.first.is_enabled():
            await step1_btn.first.click()
            await page.wait_for_timeout(2500)

        step4_btn = page.locator("#nextagenda_step4")
        if await step4_btn.count() > 0 and await step4_btn.first.is_visible():
            await step4_btn.first.click()
            await page.wait_for_timeout(6000)
            await page.screenshot(path=CONFIRM_SCREENSHOT, full_page=True)
            body_after = await page.inner_text("body")
            if re.search(r"confirmaci[óo]n|exitosa|asignada|reservada|agendada", body_after, re.I):
                return True, "Cita agendada y confirmada exitosamente por interfaz web."
            return True, "Formulario de confirmación enviado."

        return False, "El botón final de confirmación (#nextagenda_step4) no estuvo habilitado."
    except Exception as e:
        return False, f"Excepción en agendamiento UI: {e}"


async def attempt_api_booking_fallback(
    page,
    auth_headers: dict,
    patient_detail: dict,
    afiliacion_data: dict,
    target_order: dict,
    winning_slot: dict,
) -> tuple[bool, str, str]:
    raw_slot = winning_slot["raw_slot"]
    payload = {
        "reserveWithOrderRequest": {
            "fechaHora": raw_slot.get("fechaHora"),
            "modalidad": "P",
            "especialidadId": target_order.get("codigoEspecialidad", "0235"),
            "prestadorId": (raw_slot.get("prestador") or {}).get("codigo", ""),
            "pacienteId": target_order.get("pacienteId", ""),
            "epsParticular": "",
            "tipoPlanificacionId": raw_slot.get("tipoPlanificacion") or raw_slot.get("planificacion") or "",
            "ordenId": target_order.get("numeroOrden", ""),
            "unidadPlanificada": raw_slot.get("unidadTratamiento") or target_order.get("unidadOrgId", ""),
            "numeroPrestacion": target_order.get("prestacionId", ""),
            "consultorio": (raw_slot.get("consultorio") or {}).get("codigo", "") or "",
        },
        "createWithOrderRequest": None,
        "patientDetail": patient_detail,
        "clientDetail": afiliacion_data,
        "specialtyDetail": {
            "codigo": target_order.get("codigoEspecialidad", "0235"),
            "descripcion": target_order.get("descripcionEspecialidad", "MEDICINA FAMILIAR"),
        },
    }
    res = await page.evaluate(
        """async ({headers, body}) => {
            try {
                const r = await fetch('https://www.saludcolsubsidio.com/api/conorden/v1.0/agendamiento/citas/conorden/reserva-confirmacion', {
                    method: 'POST',
                    headers: {...headers, 'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                const txt = await r.text();
                return {status: r.status, text: txt};
            } catch (err) {
                return {status: 0, text: String(err)};
            }
        }""",
        {"headers": auth_headers, "body": payload},
    )
    try:
        data = json.loads(res.get("text") or "{}")
        cod = ((data.get("resultado") or [{}])[0]).get("codigo", "")
        desc = ((data.get("resultado") or [{}])[0]).get("descripcion", "")
        creacion = (data.get("creacionConOrden") or [{}])[0]
        reserva_id = ((creacion.get("cita") or {}).get("idReserva")) or ""
        if cod == "I" and reserva_id:
            return True, str(reserva_id), f"Confirmada por API directa ({desc})"
        return False, "", f"Respuesta API ({res.get('status')}): código={cod}, {desc}"
    except Exception:
        return False, "", f"Respuesta API ({res.get('status')})"


async def run_check():
    cfg = load_config()
    if not (cfg["username"] and cfg["password"]):
        msg = "Faltan credenciales COLSUBSIDIO_USER / COLSUBSIDIO_PASS."
        print(f"[ERROR] {msg}")
        write_report("❌ Error de configuración", [msg])
        sys.exit(1)

    state = load_state()
    notified_hashes = set(state.get("notified_other_hashes") or [])
    print(f"[{now_bogota().strftime('%Y-%m-%d %H:%M:%S')}] Iniciando verificación en Portal Salud Colsubsidio...")

    captured_auth_headers = {}
    captured_patient = {}
    captured_patient_detail = {}
    captured_afiliacion = {}
    captured_orders = []
    captured_reserva_response = {}

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            locale="es-CO",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = await context.new_page()

        async def on_response(response):
            nonlocal captured_auth_headers, captured_patient, captured_patient_detail, captured_afiliacion, captured_orders, captured_reserva_response
            try:
                url = response.url
                if "saludcolsubsidio.com/api/" not in url:
                    return
                req_h = response.request.headers
                if req_h.get("authorization"):
                    captured_auth_headers = {
                        k: v
                        for k, v in req_h.items()
                        if k.lower() in ("authorization", "ocp-apim-subscription-key", "accept")
                    }
                if response.status == 200:
                    if "consultaPaciente?" in url:
                        captured_patient = await response.json()
                    elif "consultaPacienteDetalle?" in url:
                        captured_patient_detail = await response.json()
                    elif "comprobacionAfiliaciones?" in url:
                        captured_afiliacion = await response.json()
                    elif "consultaOrdenesPendientes?" in url:
                        captured_orders = await response.json()
                    elif "reserva-confirmacion" in url:
                        captured_reserva_response = await response.json()
            except Exception:
                pass

        page.on("response", on_response)

        # 1. Login SSO Colsubsidio
        await page.goto("https://www.saludcolsubsidio.com", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector("#username", timeout=30000)
        await page.wait_for_timeout(1200)
        await page.select_option("#documentTypePerson", value=cfg["doc_type_option"])
        await page.fill("#username", cfg["username"])
        await page.fill("#password", cfg["password"])
        await page.wait_for_timeout(400)
        await page.click("button.button-next")

        # 2. Seleccionar Afiliados a Famisanar
        await page.wait_for_selector("button:has-text('Ingresar')", timeout=35000)
        await page.wait_for_timeout(4000)
        await page.locator("button:has-text('Ingresar')").nth(0).click()

        # 3. Omitir encuesta emergente si aparece
        try:
            await page.wait_for_selector("button:has-text('Omitir')", timeout=12000)
            await page.locator("button:has-text('Omitir')").first.click()
        except Exception:
            pass

        # 4. Ir a Órdenes Pendientes
        await page.wait_for_selector("#btnmenuLateral23", timeout=25000)
        await page.wait_for_timeout(1500)
        await page.click("#btnmenuLateral23")
        await page.wait_for_timeout(5000)

        # 5. Identificar la orden activa de Medicina Familiar
        familiar_orders = [
            o
            for o in (captured_orders or [])
            if "familiar" in normalize_text(o.get("descripcionEspecialidad", ""))
            or str(o.get("codigoEspecialidad", "")) == "0235"
        ]

        if not familiar_orders:
            await browser.close()
            if state.get("booked"):
                msg = "La orden de Medicina Familiar ya fue agendada exitosamente con la Dra. Yuliet Cárdenas."
                print(f"[OK] {msg}")
                write_report("✅ Cita ya agendada con la Dra. Yuliet Cárdenas", [msg])
                return
            msg = "No aparece ninguna orden pendiente de Medicina Familiar en Órdenes Pendientes."
            print(f"[INFO] {msg}")
            write_report("ℹ️ Sin orden pendiente de Medicina Familiar", [msg])
            return

        target_order = familiar_orders[0]
        esp_id = str(target_order.get("codigoEspecialidad", "0235"))
        prestacion_id = str(target_order.get("prestacionId", "890202232A"))
        num_orden = str(target_order.get("numeroOrden", ""))
        masked_orden = mask_id(num_orden)
        desc_serv = str(target_order.get("descripcionPrestacion", ""))
        paciente_id = str(target_order.get("pacienteId") or captured_patient.get("id") or "")
        convenio = str(((captured_afiliacion.get("derechos") or {}).get("convenioCodigo")) or "110010817105")

        print(f"[OK] Orden localizada: Medicina Familiar ({esp_id}) | Orden {masked_orden}")

        # 6. Verificar si ya existe cita agendada para esta prestación (evita doble agendamiento)
        existing_check = await page.evaluate(
            """async ({headers, docType, docNum, espId, prestacion}) => {
                const u = `https://www.saludcolsubsidio.com/api/pacientes/v1.0/pacientes/consultaCitasMedicasEspecializacion?tipoId=${encodeURIComponent(docType)}&numId=${encodeURIComponent(docNum)}&espId=${encodeURIComponent(espId)}&prestacion=${encodeURIComponent(prestacion)}`;
                const r = await fetch(u, {headers: {...headers, 'Cache-Control': 'no-cache'}});
                return r.ok ? await r.json() : {};
            }""",
            {
                "headers": captured_auth_headers,
                "docType": cfg["doc_type_code"],
                "docNum": cfg["username"],
                "espId": esp_id,
                "prestacion": prestacion_id,
            },
        )
        if int(existing_check.get("totalCountPrestacion") or 0) > 0:
            await browser.close()
            msg = f"Ya existe una cita activa agendada para Medicina Familiar (Orden {masked_orden}). No se realiza ningún cambio."
            print(f"[OK] {msg}")
            write_report("✅ Cita ya agendada en el sistema", [msg])
            return

        # 7. Entrar a la pantalla de disponibilidad de Medicina Familiar
        await page.evaluate(
            """() => {
                const cards = Array.from(document.querySelectorAll('.card-padding'));
                const fam = cards.find(c => /medicina familiar/i.test(c.innerText || ''));
                if (fam) {
                    const btn = fam.querySelector('button.buttom-orden, button');
                    if (btn) btn.click();
                }
            }"""
        )
        await page.wait_for_selector("button:has-text('Agendar Cita')", timeout=20000)
        await page.wait_for_timeout(1200)
        await page.locator("button:has-text('Agendar Cita')").first.click()
        await page.wait_for_timeout(5000)

        # 8. Escanear todas las ciudades, sedes, jornadas, fechas y especialistas disponibles
        scan_result = await page.evaluate(
            """async ({headers, espId, prestacion, pacienteId, convenio}) => {
                const h = {...headers, 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'};
                const base = 'https://www.saludcolsubsidio.com/api/informacion/v1.0/agendamiento/informacion/';

                const citiesUrl = `${base}centrosmedicos/ciudades?espId=${encodeURIComponent(espId)}&prestacion=${encodeURIComponent(prestacion)}&identificacion=${encodeURIComponent(pacienteId)}&codigoConvenio=${encodeURIComponent(convenio)}&tipoCita=orden-clinica&tipoPaciente=pos&tipoCobertura=C`;
                const citiesRes = await (await fetch(citiesUrl, {headers: h})).json();
                const rawCities = citiesRes.obtenerIps || [];
                if (!rawCities.length) {
                    return {citiesCount: 0, centersCount: 0, allSlots: []};
                }

                const seenCityIds = new Set();
                const cities = [];
                for (const item of rawCities) {
                    const cid = item?.direccion?.ciudad?.id || item?.id || '';
                    const cname = item?.direccion?.ciudad?.nombre || item?.nombre || 'BOGOTA';
                    if (cid && !seenCityIds.has(cid)) {
                        seenCityIds.add(cid);
                        cities.push({id: cid, nombre: cname});
                    }
                }

                const allSlots = [];
                let centersCount = 0;

                for (const city of cities) {
                    const mcUrl = `${base}centrosmedicos?ciudad=${encodeURIComponent(city.id)}&especialidad=${encodeURIComponent(espId)}&codigoConvenio=${encodeURIComponent(convenio)}&prestacion=${encodeURIComponent(prestacion)}&identificacion=${encodeURIComponent(pacienteId)}&tipoCita=orden-clinica&tipoPaciente=pos&tipoCobertura=C`;
                    const mcRes = await (await fetch(mcUrl, {headers: h})).json();
                    const centers = mcRes.obtenerIps || [];
                    centersCount += centers.length;

                    for (const center of centers) {
                        const codeIps = center.codigo || '';
                        const ipsName = center.nombre || codeIps;
                        const ipsAddress = center?.direccion?.descripcion || center?.direccion?.direccion || '';
                        if (!codeIps) continue;

                        for (const jornada of ['M', 'T']) {
                            const dispUrl = `${base}disponibilidad?especialidad=${encodeURIComponent(espId)}&paciente=${encodeURIComponent(pacienteId)}&medico=&ipsadscripcion=&jornada=${jornada}&planificacion=&ips=${encodeURIComponent(codeIps)}&codigo=${encodeURIComponent(prestacion)}&appointmentMoreNext=C&cityId=`;
                            let dispRes = {};
                            try {
                                dispRes = await (await fetch(dispUrl, {headers: h})).json();
                            } catch (e) {
                                continue;
                            }
                            const fechas = dispRes.disponibilidad || [];
                            for (const fObj of fechas) {
                                const fecha = fObj.fecha || '';
                                if (!fecha) continue;
                                const fechaCompacta = fecha.replace(/-/g, '');
                                const horUrl = `${base}horario?especialidad=${encodeURIComponent(espId)}&paciente=${encodeURIComponent(pacienteId)}&fechasolicitud=${fechaCompacta}&medico=&jornada=${jornada}&planificacion=&ips=${encodeURIComponent(codeIps)}&codigo=${encodeURIComponent(prestacion)}&appointmentMoreNext=C&cityId=`;
                                let horRes = {};
                                try {
                                    horRes = await (await fetch(horUrl, {headers: h})).json();
                                } catch (e) {
                                    continue;
                                }
                                const horarios = horRes.citaHorario || [];
                                for (const slot of horarios) {
                                    const pCode = (slot?.prestador?.codigo || '').replace('null', '').trim();
                                    const pName = (slot?.prestador?.nombre || '').trim();
                                    if (!pCode) continue;
                                    allSlots.push({
                                        city_id: city.id,
                                        city_name: city.nombre,
                                        code_ips: codeIps,
                                        ips_name: ipsName,
                                        ips_address: ipsAddress,
                                        jornada,
                                        fecha,
                                        hora_raw: slot.hora || '',
                                        fecha_hora: slot.fechaHora || '',
                                        doctor_code: pCode,
                                        doctor_name: pName,
                                        raw_slot: slot
                                    });
                                }
                            }
                        }
                    }
                }
                return {citiesCount: cities.length, centersCount, allSlots};
            }""",
            {
                "headers": captured_auth_headers,
                "espId": esp_id,
                "prestacion": prestacion_id,
                "pacienteId": paciente_id,
                "convenio": convenio,
            },
        )

        all_slots = scan_result.get("allSlots") or []
        cities_count = scan_result.get("citiesCount", 0)
        centers_count = scan_result.get("centersCount", 0)

        # Separar cupos de la Dra. Yuliet Cárdenas vs otros médicos
        yuliet_slots = [s for s in all_slots if matches_yuliet_cardenas(s.get("doctor_name", ""))]
        other_slots = [s for s in all_slots if not matches_yuliet_cardenas(s.get("doctor_name", ""))]

        # CASO A: NO HAY NINGÚN CUPO CON NADIE
        if not yuliet_slots and not other_slots:
            await browser.close()
            status_msg = "⏳ Sin agenda habilitada todavía (0 cupos abiertos en Medicina Familiar)"
            details = [
                f"Orden activa verificada: **{masked_orden}** (*Medicina Familiar*).",
                "El portal responde actualmente: *\"De momento ese servicio no está disponible por este canal\"*.",
                "Se volverá a revisar automáticamente en el siguiente ciclo.",
            ]
            print(f"[SIN CUPOS] Aún no hay agenda abierta para Medicina Familiar (Orden {masked_orden}).")
            write_report(status_msg, details)
            return

        # CASO B: HAY CITA CON OTRO(S) MÉDICO(S), PERO NO CON YULIET CÁRDENAS -> ¡AVISAR PERO NO AGENDAR!
        if not yuliet_slots and other_slots:
            await browser.close()
            other_slots.sort(key=lambda x: (x["fecha"], x["hora_raw"]))

            # Agrupar por (doctor, sede, fecha) para mostrar resumen claro y evitar correos repetidos
            grouped = {}
            new_groups = {}
            for s in other_slots:
                _, h_ap = format_12h(s["hora_raw"])
                key_tuple = (s["doctor_name"], s["ips_name"], s["fecha"])
                grouped.setdefault(key_tuple, []).append(h_ap)

                sig_raw = f"{s['doctor_name']}|{s['code_ips']}|{s['fecha']}"
                sig_hash = hashlib.sha256(sig_raw.encode("utf-8")).hexdigest()[:16]
                if sig_hash not in notified_hashes:
                    notified_hashes.add(sig_hash)
                    new_groups[key_tuple] = grouped[key_tuple]

            other_doctors_names = sorted({s["doctor_name"] for s in other_slots if s.get("doctor_name")})
            status_msg = (
                f"🔔 Hay cupos con otro médico ({len(other_slots)} horarios), pero 0 con la Dra. Yuliet Cárdenas — NO se agendó"
            )
            details = [
                f"Orden activa verificada: **{masked_orden}**.",
                f"Centros médicos con agenda: {centers_count} | Horarios totales con otros médicos: {len(other_slots)}.",
                f"Otros profesionales disponibles: **{', '.join(other_doctors_names[:8])}**.",
                "**Acción tomada:** NO se agendó automáticamente (reservado exclusivamente para la Dra. Yuliet Cárdenas).",
            ]

            if new_groups:
                # Construir listado legible para correo y alerta en Windows
                lines_plain = []
                rows_html = []
                for (doc_n, ips_n, fec), hrs in list(new_groups.items())[:12]:
                    hrs_preview = ", ".join(dict.fromkeys(hrs))[:90]
                    lines_plain.append(f"• {doc_n} | {fec} ({hrs_preview}) | {ips_n}")
                    rows_html.append(
                        f"<li><b>{doc_n}</b> — Fecha: <b>{fec}</b> ({hrs_preview}) — Sede: {ips_n}</li>"
                    )

                if cfg["windows_popup"]:
                    win_msg = (
                        "Abrieron agenda de Medicina Familiar con OTRO médico.\n"
                        "NO se agendó (seguimos esperando a la Dra. Yuliet Cárdenas):\n\n"
                        + "\n".join(lines_plain[:6])
                    )
                    trigger_windows_alert("Aviso Medicina Familiar (Otro Médico)", win_msg, urgent=False)

                subj = f"ℹ️ Cupos en Medicina Familiar con otro médico (NO agendado — esperando a Dra. Yuliet)"
                html_other = f"""<!DOCTYPE html>
<html lang="es">
<body style="font-family: system-ui, sans-serif; background:#f8fafc; padding:24px;">
  <div style="max-width:640px; margin:0 auto; background:#ffffff; border-radius:12px; border:1px solid #cbd5e1; overflow:hidden;">
    <div style="background:#0369a1; color:#ffffff; padding:18px 20px;">
      <h2 style="margin:0; font-size:19px;">ℹ️ Aviso: Abrieron agenda con otro profesional (NO agendado)</h2>
    </div>
    <div style="padding:20px; color:#1e293b; line-height:1.6;">
      <p>Se detectaron cupos abiertos en <b>Medicina Familiar</b> (Orden #{num_orden}), pero corresponden a <b>otro(s) médico(s)</b>:</p>
      <ul>
        {''.join(rows_html)}
      </ul>
      <p style="background:#fef9c3; border-left:4px solid #ca8a04; padding:12px; border-radius:6px;">
        🔒 <b>No se agendó ninguna cita:</b> El bot dejó tu orden intacta y sigue revisando automáticamente para agendar <b>solo con la Dra. Yuliet Cárdenas</b>. (Si en algún momento deseas tomar alguno de estos otros cupos manualmente, puedes entrar al portal).
      </p>
      <p style="font-size:13px; color:#64748b;">Verificado el {now_bogota().strftime('%Y-%m-%d %I:%M:%S %p')} (Hora Colombia).</p>
    </div>
  </div>
</body>
</html>"""
                _, mail_msg = send_email_notification(cfg, subj, html_other)
                details.append(f"Aviso de otros médicos: {mail_msg}")
                state["notified_other_hashes"] = sorted(list(notified_hashes))[-200:]
                save_state(state)
                print(f"[AVISO OTROS MEDICOS] {len(new_groups)} combinaciones nuevas notificadas sin agendar. ({mail_msg})")
            else:
                details.append("Ya se había enviado aviso previo sobre estos mismos cupos de otros médicos (sin spam).")
                print("[SIN YULIET] Siguen disponibles los mismos cupos con otros médicos ya notificados; esperando a Dra. Yuliet.")

            write_report(status_msg, details)
            return

        # CASO C: ¡HAY CUPO CON LA DRA. YULIET CÁRDENAS! -> AGENDAR AUTOMÁTICAMENTE EL PRIMERO
        yuliet_slots.sort(key=lambda x: (x["fecha"], x["hora_raw"]))
        winner = yuliet_slots[0]
        winner["hora_24"], winner["hora_ap"] = format_12h(winner["hora_raw"])

        print(
            f"🎉 ¡CUPO ENCONTRADO CON {winner['doctor_name']}! "
            f"Fecha: {winner['fecha']} {winner['hora_ap']} | Sede: {winner['ips_name']}"
        )

        booked_ok = False
        booking_note = "Solo notificación (auto_book desactivado)."
        reserva_id = ""

        if cfg["auto_book"]:
            ui_ok, ui_msg = await attempt_ui_booking(page, winner)
            if captured_reserva_response:
                cod = ((captured_reserva_response.get("resultado") or [{}])[0]).get("codigo", "")
                creacion = (captured_reserva_response.get("creacionConOrden") or [{}])[0]
                reserva_id = str(((creacion.get("cita") or {}).get("idReserva")) or "")
                if cod == "I" and reserva_id:
                    booked_ok = True
                    booking_note = f"Cita agendada automáticamente (Reserva #{reserva_id})."

            if not booked_ok:
                api_ok, api_res_id, api_msg = await attempt_api_booking_fallback(
                    page,
                    captured_auth_headers,
                    captured_patient_detail or captured_patient,
                    captured_afiliacion,
                    target_order,
                    winner,
                )
                if api_ok:
                    booked_ok = True
                    reserva_id = api_res_id
                    booking_note = api_msg
                else:
                    booking_note = f"{ui_msg} | {api_msg}"

            try:
                await page.screenshot(path=CONFIRM_SCREENSHOT, full_page=True)
            except Exception:
                pass

        await browser.close()

        if booked_ok:
            state["booked"] = True
            state["booked_at"] = now_bogota().isoformat()
            state["fecha"] = winner["fecha"]
            state["hora"] = winner["hora_ap"]
            state["sede"] = winner["ips_name"]
            save_state(state)

        status_header = (
            f"✅ ¡CITA AGENDADA CON LA DRA. {winner['doctor_name']}!"
            if booked_ok
            else f"🚨 ¡CUPO DISPONIBLE CON LA DRA. {winner['doctor_name']}! (Ingresa ya para confirmar)"
        )
        details_report = [
            f"**Profesional:** {winner['doctor_name']}",
            f"**Fecha y hora:** {winner['fecha']} a las {winner['hora_ap']}",
            f"**Centro Médico:** {winner['ips_name']}",
            f"**Orden médica:** {masked_orden}",
            f"**Estado:** {'Agendada exitosamente' if booked_ok else 'Pendiente confirmación manual'}",
        ]

        if cfg["windows_popup"]:
            win_title = (
                "¡Cita Agendada con Dra. Yuliet Cárdenas!"
                if booked_ok
                else "¡Cupo Disponible con Dra. Yuliet Cárdenas!"
            )
            win_body = (
                f"{status_header}\n\n"
                f"Doctora: {winner['doctor_name']}\n"
                f"Fecha: {winner['fecha']} - {winner['hora_ap']}\n"
                f"Sede: {winner['ips_name']}\n"
                f"Orden: #{num_orden}\n"
                f"Detalle: {booking_note}"
            )
            trigger_windows_alert(win_title, win_body, urgent=True)

        email_subject = (
            f"✅ Cita Agendada con Dra. {winner['doctor_name']} ({winner['fecha']} {winner['hora_ap']})"
            if booked_ok
            else f"🚨 ¡Cupo Disponible con Dra. {winner['doctor_name']} en Salud Colsubsidio!"
        )
        email_html = f"""<!DOCTYPE html>
<html lang="es">
<body style="font-family: system-ui, sans-serif; background:#f8fafc; padding:24px;">
  <div style="max-width:640px; margin:0 auto; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; overflow:hidden;">
    <div style="background:{'#15803d' if booked_ok else '#b45309'}; color:#ffffff; padding:20px;">
      <h2 style="margin:0; font-size:20px;">{status_header}</h2>
    </div>
    <div style="padding:20px; color:#1e293b; line-height:1.6;">
      <p>El monitor automático detectó disponibilidad con la <b>Dra. Yuliet Cárdenas</b> en <b>Salud Colsubsidio</b>:</p>
      <ul>
        <li><b>Doctora:</b> {winner['doctor_name']}</li>
        <li><b>Fecha:</b> {winner['fecha']}</li>
        <li><b>Hora:</b> {winner['hora_ap']}</li>
        <li><b>Centro Médico:</b> {winner['ips_name']} ({winner['ips_address'] or winner['city_name']})</li>
        <li><b>Número de Orden:</b> {num_orden} ({desc_serv})</li>
        <li><b>Estado de Reserva:</b> {booking_note}</li>
      </ul>
      <p style="font-size:13px; color:#64748b;">Verificado el {now_bogota().strftime('%Y-%m-%d %I:%M:%S %p')} (Hora Colombia).</p>
    </div>
  </div>
</body>
</html>"""
        mail_ok, mail_msg = send_email_notification(
            cfg,
            email_subject,
            email_html,
            CONFIRM_SCREENSHOT if os.path.exists(CONFIRM_SCREENSHOT) else None,
        )
        details_report.append(f"**Confirmación por correo:** {mail_msg}")
        print(f"[CORREO] {mail_msg}")

        write_report(status_header, details_report)


if __name__ == "__main__":
    asyncio.run(run_check())
