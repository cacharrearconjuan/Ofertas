# 🔥 Radar de chollos

Vigila **Chollometro** y avisa en un **grupo de Telegram** de las **ofertas y cupones** de tecnología y gaming que la comunidad ha votado por encima de **200°**: PS5, mandos, SSD y M.2 de todo tipo, discos, portátiles y PC, Fire TV, Nintendo, Xbox, consolas portátiles, saldo, monitores, gráficas, auriculares y redes.

- **Palabras en un fichero:** `palabras.txt` se edita desde la web de GitHub, también desde el móvil.
- **Keepa en las ofertas de Amazon:** gráfica del historial de precios y botón a Keepa. Con la clave de la API de Keepa, además, un veredicto automático: 🟢 rebaja real, 🟡 poca rebaja o 🔴 no es rebaja.
- **Avisos con foto y botones:** Chollometro, la tienda y Keepa. Se pueden repartir por **temas** del grupo y llegan sin sonido de noche.
- **Web** con las destacadas y las que se están calentando. Se puede añadir a la pantalla de inicio del móvil.
- **Gratis:** GitHub Actions en un repositorio público, cada ~10 min.

Una oferta nace a 0° y va subiendo. El radar sigue las que encajan con tus palabras durante 72 h y avisa **una sola vez**, cuando pasan del mínimo. Descarta hilos de discusión y ofertas caducadas. Los cupones salen marcados con 🎟️ (se pueden quitar con `incluir_cupones: false`).

**¿Amazon u otra tienda?** Chollometro dice en cada oferta de qué tienda es, y el aviso lo pone en la primera línea: 🟠 **AMAZON**, 🔴 **ALIEXPRESS**, 🏪 el resto.

---

## Puesta en marcha

### Paso 1 · Crear el bot de Telegram
1. En Telegram busca **@BotFather** (con la marca azul de verificado) y pulsa **Iniciar**.
2. Escribe `/newbot`.
3. **Nombre** (el que se ve): por ejemplo `Radar de chollos`.
4. **Usuario**: tiene que acabar en `bot`, por ejemplo `radar_chollos_bot`. Si está cogido, prueba otro.
5. BotFather te da el **token**, algo como `7123456789:AAH...`. **Cópialo y guárdalo**: es la contraseña del bot.
6. *(Opcional)* Ponle foto con `/setuserpic` y descripción con `/setdescription`.

> Usa un bot **nuevo** solo para esto, no otro que ya tengas funcionando: si ese bot tiene un webhook, el paso 4 no funcionaría.

### Paso 2 · Crear el grupo
1. Telegram → lápiz / **Nuevo grupo** → añade **tu bot** buscándolo por su usuario → ponle nombre (p. ej. `🔥 Chollos`) → **Crear**.
2. *(Opcional, recomendado)* **Temas**, para tener cada categoría separada:
   - Abre el grupo → nombre del grupo → **Editar** (lápiz) → activa **Temas**.
   - Crea los temas que quieras (p. ej. `💾 SSD y discos`, `🎮 PS5`, `💻 Portátiles`). Lo que no tenga tema va a **General**.
3. Haz al bot **administrador**: grupo → **Editar** → **Administradores** → **Añadir administrador** → tu bot. No hace falta darle ningún permiso especial. Así nunca le bloquean los mensajes, tampoco en los temas.

### Paso 3 · Despertar al bot en el grupo
Escribe en el grupo (en **General** si usas temas):
```
/start@radar_chollos_bot
```
(con el usuario de tu bot). El bot no responde, es normal.

### Paso 4 · Sacar el id del grupo
1. En el navegador abre esta dirección, poniendo tu token:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Ejemplo: `https://api.telegram.org/bot7123456789:AAH.../getUpdates`
2. Busca `"chat":{"id":-100…`. Ese número, **con el guion delante**, es el id del grupo. Ejemplo: `-1002233445566`.
3. Si sale `"result":[]`, vuelve a escribir `/start@tu_bot` en el grupo y recarga la página.

> ⚠️ Si activas los **temas** después de este paso, el id cambia: repite los pasos 3 y 4.

### Paso 5 · *(Opcional)* Sacar el número de cada tema
Abre un tema → mantén pulsado un mensaje → **Copiar enlace**. Saldrá algo como:
`https://t.me/c/2233445566/`**`77`**`/80` → el número del tema es el del medio: **77**.
Ponlo en `palabras.txt`, en la categoría que quieras: `tema: 77`.

### Paso 6 · Meter las claves en GitHub
En el repositorio: **Settings → Secrets and variables → Actions → New repository secret**:

| Nombre | Valor | |
|---|---|---|
| `TELEGRAM_TOKEN` | el token del paso 1 | obligatorio |
| `TELEGRAM_CHAT_ID` | el id del paso 4, con el guion | obligatorio |
| `TELEGRAM_THREAD_ID` | un tema para todo lo que no tenga tema propio | opcional |
| `KEEPA_API_KEY` | la clave de la API de Keepa (de pago) | opcional |

Los secretos no los ve nadie aunque el repositorio sea público.

### Paso 7 · Probar
**Actions → Radar de chollos → Run workflow** → elige el modo → **Run workflow**:
1. **prueba**: tiene que llegar «🧪 Prueba del radar de chollos» al grupo, con un botón.
2. **diagnostico**: entra en la ejecución → **radar** → **Buscar chollos** y mira el registro. Comprueba que salen ofertas **con grados** (no `None°`) y «Grados leídos: 3/3». Si todo sale `None°` o con ERROR, mira el **plan B**.
3. **ronda**: la primera es silenciosa (guarda lo que ya está caliente para no inundarte) y manda «✅ Radar en marcha». A partir de ahí va solo cada 10 minutos.

### Paso 8 · Activar la web
Después de la primera ronda: **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `gh-pages` / `(root)` → Save**.
Al minuto estará en **https://cacharrearconjuan.github.io/Ofertas/**. En el móvil: menú del navegador → **Añadir a pantalla de inicio**.

---

## Añadir o quitar palabras

Abre **`palabras.txt`** en GitHub → **lápiz** → cambia → **Commit changes**. Se aplica en la siguiente ronda.

```
[💾 SSD y discos]          ← categoría
ssd, nvme, m.2             ← entra si el título lo contiene
-macbook                   ← fuera si lo contiene
+ssd portatil              ← entra siempre, aunque haya una palabra con -
grados: 150                ← mínimo propio de esta categoría
precio max: 200            ← solo hasta 200 €
tema: 77                   ← a este tema del grupo
aviso: sata => ℹ️ texto    ← nota en el aviso si el título dice "sata"
apagada                    ← desactiva la categoría
```

Al guardar se ejecuta **Comprobar palabras**. Si sale en **verde**, el fichero se entiende bien. Si sale en **rojo**, abre la ejecución y te dice qué línea falla. También puedes lanzar el modo **palabras** a mano para ver cómo lo entiende.

---

## Keepa: ¿es una rebaja de verdad?
- **Sin clave** (gratis): las ofertas de Amazon llevan el botón **📈 Keepa** al historial de precios y, si Keepa la sirve, la gráfica de 90 días como foto.
- **Con clave** (`KEEPA_API_KEY`, plan de pago de la API de Keepa): además sale un veredicto:
  - 🟢 mínimo de 90 días, o rebaja de al menos un 10 % sobre la media de 90 días
  - 🟡 algo de rebaja, pero menos de lo que pides
  - 🔴 no es rebaja

  Cada oferta gasta 1 token, más 1 si hay gráfica.
- En `config.json` → `keepa`:
  - `rebaja_minima_pct`: el % que pides para el 🟢.
  - `descartar_falsas: true`: no te avisa de las 🔴.

---

## Ajustes generales (`config.json`)
| Clave | Para qué |
|---|---|
| `grados_minimos` | mínimo general de grados (200) |
| `tiendas_permitidas` | vacío = todas; p. ej. `["Amazon", "AliExpress"]` |
| `temas_por_tienda` | manda cada tienda a su tema del grupo, p. ej. `{"Amazon": 12, "AliExpress": 13}` (gana al tema de la categoría) |
| `incluir_cupones` | `true` = también cupones; `false` = solo ofertas |
| `silencio` | de 00:30 a 08:00 los avisos llegan sin sonido |
| `horas_seguimiento` | cuánto se espera a que una oferta se caliente (72 h) |
| `max_avisos_por_ronda` | tope de avisos cada 10 min (10); el resto sale en la siguiente ronda |

---

## Que se ejecute solo de verdad (cron-job.org)

La programación de GitHub (`schedule`) a veces tarda horas en arrancar en repositorios nuevos, y en horas punta se salta ejecuciones. Para que vaya como un reloj, lánzalo cada 10 minutos desde **cron-job.org**:

1. **Token de GitHub:** avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
   - Nombre: `radar-chollos` · Caducidad: la que quieras (p. ej. 1 año).
   - **Repository access → Only select repositories → `Ofertas`**.
   - **Permissions → Repository permissions → Actions: Read and write**. Nada más.
   - **Generate token** y cópialo (`github_pat_…`).
2. **cron-job.org → Create cronjob:**
   - URL: `https://api.github.com/repos/cacharrearconjuan/Ofertas/actions/workflows/radar.yml/dispatches`
   - Horario: **cada 10 minutos**.
   - Pestaña **Advanced**:
     - Request method: **POST**
     - Headers:
       - `Authorization` → `Bearer github_pat_…`
       - `Accept` → `application/vnd.github+json`
       - `X-GitHub-Api-Version` → `2022-11-28`
     - Request body: `{"ref":"main","inputs":{"modo":"ronda"}}`
   - Guarda y pulsa **Test run**: la respuesta correcta es **204** y en GitHub → Actions aparece una ejecución nueva.

Si además GitHub se pone a lanzarlo por su cuenta, no pasa nada: las ejecuciones van una detrás de otra y nunca avisan dos veces de la misma oferta.

---

## Plan B: en un servidor de casa
Si Chollometro bloquea a GitHub, el mismo script funciona en cualquier Linux con Python 3.9+, sin instalar nada. Solo hace conexiones de salida, así que el CG-NAT no molesta:

```bash
git clone https://github.com/cacharrearconjuan/Ofertas /opt/radar
cat > /opt/radar/.env <<'EOF'
TELEGRAM_TOKEN=7123456789:AAH...
TELEGRAM_CHAT_ID=-100...
EOF
chmod 600 /opt/radar/.env
cd /opt/radar && set -a && . ./.env && set +a && python3 radar.py --prueba
( crontab -l 2>/dev/null; echo '*/10 * * * * cd /opt/radar && git pull -q; set -a && . ./.env && set +a && python3 radar.py >> radar.log 2>&1' ) | crontab -
```
Con `git pull` en el cron, lo que cambies en `palabras.txt` desde GitHub también llega al servidor. Si lo pasas a casa, desactiva la tarea en GitHub (**Actions → Radar de chollos → ⋯ → Disable workflow**) para no recibir avisos repetidos.

---

## Cosas a saber
- GitHub puede retrasar las ejecuciones programadas unos minutos.
- En repositorios públicos, GitHub desactiva las tareas programadas tras 60 días sin actividad. Cada ronda publica en `gh-pages`, que en principio cuenta como actividad. Si se desactivara, se reactiva con un clic en **Actions**.
- Si en unas 2 horas no consigue leer Chollometro, el bot manda **un** aviso al grupo (como mucho uno al día).
- Usa las fuentes públicas de Chollometro con moderación. No bajes la frecuencia de 10 minutos.
