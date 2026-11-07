# 🩺 Monitor y Agendador Automático — Salud Colsubsidio (Dra. Yuliet Cárdenas)

Este bot ingresa al **Portal Salud Colsubsidio**, revisa **Órdenes Pendientes → Medicina familiar** cada 15 minutos, busca disponibilidad exclusivamente con la **Dra. Yuliet Cárdenas** y, apenas se habilite un cupo:
1. **Agenda automáticamente el primer horario disponible** con la Dra. Yuliet Cárdenas (nunca agenda con otro médico).
2. **Muestra alerta emergente con sonido en Windows** (cuando corre en tu PC).
3. **Envía confirmación inmediata por correo electrónico** con los datos de la sede, fecha, hora y número de reserva.

> 🔒 **Seguridad:** Ningún usuario ni contraseña está escrito en el código fuente. En tu PC se leen desde `~/.gemini/config/colsubsidio_salud_secret.json` (fuera de esta carpeta) y en GitHub se leen desde **GitHub Encrypted Secrets**.

---

## ☁️ Cómo activarlo en GitHub Actions en un Repositorio PRIVADO (Oculto)

Sigue estos pasos para que también revise cada 15 minutos cuando tu computador esté apagado, manteniendo todo **100% privado**:

1. Entra a [github.com/new](https://github.com/new):
   - **Repository name:** `bot-salud-colsubsidio`
   - **Visibility:** Selecciona obligatoriamente **🔒 Private** *(así nadie más podrá ver el repositorio)*.
   - Haz clic en **Create repository**.
2. Sube los archivos de esta carpeta (`verificar_cita_colsubsidio.py`, `requirements.txt`, `.gitignore` y la carpeta `.github/workflows/verificar_cita.yml`).
3. En tu repositorio privado ve a **Settings → Secrets and variables → Actions → New repository secret** y agrega estos secretos cifrados:
   - `COLSUBSIDIO_USER`: tu número de documento
   - `COLSUBSIDIO_PASS`: tu contraseña del portal Colsubsidio
   - `SMTP_SENDER`: tu correo Gmail remitente (el mismo que usas en el bot de apartamentos)
   - `SMTP_APP_PASSWORD`: tu contraseña de aplicación de 16 letras de Gmail
   - `SMTP_RECIPIENT`: correos donde quieres recibir la confirmación (separados por coma, ej. `manuelfernandolesmes@gmail.com, anafqch@hotmail.com`)
4. Ve a la pestaña **Actions**, habilita los workflows si te lo pide y pulsa **Monitor Cita Dra. Yuliet Cárdenas → Run workflow** para probar la primera ejecución en la nube.
