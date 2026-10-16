# DAX Impresión · agente e instalador para Windows

Programa que corre en el computador del local y manda a las impresoras térmicas lo que deja el POS DAX: facturas (con CUFE y QR),
comandas, estados de cuenta y comprobantes de domicilio. Arranca solo al encender el computador, aunque nadie inicie sesión, y
se vuelve a abrir si se cae.

## Instalar en el local (una sola vez)

1. **La impresora en Windows.** Conecta la impresora por USB y deja que Windows la instale (o instala el controlador del fabricante,
   «POS-80»). Si Windows no la reconoce, agrégala con el controlador «Generic / Text Only»: el agente le envía los datos en crudo
   (ESC/POS), así que cualquier controlador sirve. Anota el **nombre exacto** que aparece en Configuración → Impresoras y escáneres
   (por ejemplo `POS-80C` o `Caja`).
2. **La impresora en el POS.** Configuración → Impresión → Nueva impresora: conexión **USB**, en «IP / puerto o nombre» el nombre exacto
   del paso 1, ancho 80 mm, modo ESC/POS, y márcala para **cuentas y facturas** (y como predeterminada si es la única).
3. **El código.** En la misma pantalla pulsa **Conectar un equipo**: da un código de 8 caracteres que vale 15 minutos.
4. **El instalador.** En el computador del local ejecuta `DAX-Impresion-Instalar-<versión>.exe` (pide permiso de administrador).
   Escribe la dirección del POS (ya viene `https://www.theburgery.shop`) y el código. Al terminar, en el POS la impresora aparece **En línea**.
5. **Prueba.** En el POS pulsa **Imprimir prueba** en la impresora.

> Mientras el instalador no esté firmado, Windows puede mostrar «Windows protegió su PC». Pulsa **Más información → Ejecutar de todas formas**.

## Qué deja instalado
- `C:\Program Files\DAX Impresion\DAXPrintAgent.exe`.
- `C:\ProgramData\DAX\`: la clave del equipo (`print-agent.json`) y el registro (`print-agent.log`). La carpeta solo la leen SYSTEM y los
  Administradores.
- Una tarea programada «DAX Print Agent» que arranca con el equipo como SYSTEM, se reabre cada minuto si se cae y nunca se detiene por tiempo.
- En el menú Inicio, carpeta **DAX Impresión**: «Estado de la impresión» y «Ver registro de impresión».

**Actualizar:** se ejecuta el instalador nuevo; conserva la conexión y no pide el código otra vez.
**Desinstalar:** «Agregar o quitar programas» → DAX Impresión. Borra la clave del equipo; revócala también en el POS (Configuración → Integraciones).

## Desarrollo
- Agente: `dax_print_agent.py` (solo biblioteca estándar de Python 3.8+). Es la misma copia que `print-agent/` del POS, donde hay pruebas
  contra la API real; aquí `tests/` cubre el modo de equipo.
- Pruebas: `python -m pytest -q`.
- Instalador: `.github/workflows/build.yml` empaqueta el agente con PyInstaller y lo envuelve con Inno Setup (`installer/DAXImpresion.iss`)
  en un Windows de GitHub. Cada envío a `main` deja el instalador como artefacto; una etiqueta `v1.2.0` lo publica como versión.
- Sin firma digital por ahora (pendiente: Azure Trusted Signing o certificado de firma de código).
