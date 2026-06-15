"""
Ñawi — Generador de PDF de solicitud (placeholder para el MVP).

Cuando un ciudadano inicia un trámite por WhatsApp no podemos recibir su PDF fácilmente,
así que Ñawi genera automáticamente una solicitud simple con los datos recolectados para
adjuntarla al trámite real de QELLQA.

Expuesto:
  generar_solicitud_pdf(datos) -> bytes   (un PDF A4 en memoria)

`datos` esperado (todas opcionales, se muestran "—" si faltan):
  nombre, dni, celular, correo, tipo_documento, asunto, dependencia, fecha, nrofolios
"""

import io
from datetime import datetime, timezone


def _val(datos: dict, clave: str) -> str:
    v = (datos or {}).get(clave)
    v = "" if v is None else str(v).strip()
    return v if v else "—"


def generar_solicitud_pdf(datos: dict) -> bytes:
    """Genera un PDF simple con los datos del trámite y devuelve sus bytes."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas

    datos = datos or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 2.5 * cm

    def line(text, size=11, bold=False, gap=0.7):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(2.5 * cm, y, text)
        y -= gap * cm

    line("GOBIERNO REGIONAL DE CUSCO", 15, bold=True, gap=0.9)
    line("Solicitud presentada vía Ñawi (asistente digital accesible)", 11, gap=1.1)

    line("DATOS DEL CIUDADANO", 12, bold=True)
    line("Nombre: " + _val(datos, "nombre"))
    line("DNI: " + _val(datos, "dni"))
    line("Celular: " + _val(datos, "celular"))
    line("Correo: " + _val(datos, "correo"))
    y -= 0.4 * cm

    line("DATOS DEL TRÁMITE", 12, bold=True)
    line("Tipo de documento: " + _val(datos, "tipo_documento"))
    line("Dependencia: " + _val(datos, "dependencia"))
    line("Asunto: " + _val(datos, "asunto"))
    line("Número de folios: " + _val(datos, "nrofolios"))
    y -= 0.6 * cm

    generado = (datos.get("fecha") or "").strip() or datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    line("Documento generado automáticamente por el asistente digital Ñawi", 9, gap=0.6)
    line("para el Gobierno Regional de Cusco.", 9, gap=0.6)
    line("Fecha y hora de generación: " + generado, 9)

    c.showPage()
    c.save()
    return buf.getvalue()


if __name__ == "__main__":
    sample = {
        "nombre": "MARSI VALERIA FIGUEROA LARRAGAN", "dni": "76601704",
        "celular": "984000000", "correo": "ejemplo@correo.com",
        "tipo_documento": "SOLICITUD", "dependencia": "GERAGRI - GERENCIA DE AGRICULTURA",
        "asunto": "Solicitud de constancia laboral", "nrofolios": "1",
    }
    out = generar_solicitud_pdf(sample)
    with open("test_solicitud.pdf", "wb") as f:
        f.write(out)
    print("PDF generado:", len(out), "bytes -> test_solicitud.pdf")
