import os
import json
from datetime import datetime
from functools import wraps
from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    session,
    flash,
)
from werkzeug.utils import secure_filename

import redis

from database.mongo_connection import get_database

app = Flask(__name__)
app.secret_key = "super_secreto_bda_2025"  # cámbialo si quieres

# ====== Configuración de subida de fotos ======
UPLOAD_FOLDER = os.path.join(app.root_path, "static", "fotos")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}


def allowed_file(filename: str) -> bool:
    """Verifica si la extensión del archivo está permitida."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ====== Conexión a Redis ======
redis_client = redis.Redis(
    host="localhost",
    port=6379,
    db=0,
    decode_responses=True  # para que los valores vengan como str, no bytes
)

# ====== Conexión a MongoDB ======
db = get_database()
concursantes_collection = db["concursantes"]
registro_votos_collection = db["registro_votos"]
usuarios_collection = db["usuarios"]   
# Crear usuarios por defecto si no existen
if usuarios_collection.count_documents({}) == 0:
    usuarios_collection.insert_many([
        {"usuario": "admin", "password": "admin123", "rol": "admin"},
        {"usuario": "publico", "password": "publico123", "rol": "publico"},
    ])

def requiere_rol(roles):
    """
    Decorador para exigir que el usuario tenga cierto rol (o uno de varios).
    roles puede ser un string ("admin") o una lista ["publico", "admin"].
    """
    if isinstance(roles, str):
        roles_lista = [roles]
    else:
        roles_lista = roles

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            rol = session.get("rol")
            if rol not in roles_lista:
                flash("Debe iniciar sesión con un perfil autorizado para acceder a esta opción.")
                return redirect(url_for("login"))
            return func(*args, **kwargs)
        return wrapper
    return decorator
@app.route("/")
def inicio():
    # Redirigimos al login como pantalla principal
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Pantalla de inicio de sesión.
    Si el login es correcto, según el rol redirige a:
      - admin -> panel de administración
      - publico -> módulo de votación pública
    """
    if request.method == "POST":
        usuario = request.form.get("usuario")
        password = request.form.get("password")

        user = usuarios_collection.find_one({
            "usuario": usuario,
            "password": password
        })

        if user:
            session["usuario"] = user["usuario"]
            session["rol"] = user["rol"]

            flash(f"Bienvenido, {user['usuario']} (perfil: {user['rol']}).")

            if user["rol"] == "admin":
                return redirect(url_for("admin_panel"))
            else:
                return redirect(url_for("mostrar_concursantes"))
        else:
            flash("Usuario o contraseña incorrectos.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    """
    Cierra la sesión actual.
    """
    session.clear()
    flash("Sesión cerrada correctamente.")
    return redirect(url_for("login"))

# ================= MÓDULO PÚBLICO =================

@app.route("/public")
def mostrar_concursantes():
    """
    Vista pública que muestra la lista de concursantes.
    Importante: NO mostramos votos_acumulados.
    """
    concursantes = list(
        concursantes_collection.find({}, {"votos_acumulados": 0, "_id": 0})
    )
    return render_template("lista_concursantes.html", concursantes=concursantes)


@app.route("/votar/<int:concursante_id>", methods=["POST"])
@requiere_rol(["publico", "admin"])
def votar(concursante_id):
    """
    Registra un voto para el concursante indicado.
    - Evita votos duplicados por concursante usando la sesión.
    - Actualiza votos_acumulados en MongoDB.
    - Registra el voto en la colección registro_votos.
    - Actualiza los votos en Redis para votación en tiempo real.
    """

    # 1. Obtener la lista de votos emitidos desde la sesión
    votos_emitidos = session.get("votos_emitidos", [])

    # 2. Verificar si ya votó por este concursante
    if concursante_id in votos_emitidos:
        flash("Ya votaste por esta persona. No puedes votar dos veces por el mismo concursante.")
        return redirect(url_for("mostrar_concursantes"))

    # 3. Actualizar votos_acumulados en la colección de concursantes (Mongo)
    resultado = concursantes_collection.update_one(
        {"id": concursante_id},
        {"$inc": {"votos_acumulados": 1}}
    )

    if resultado.matched_count == 0:
        flash("El concursante seleccionado no existe.")
        return redirect(url_for("mostrar_concursantes"))

    # 4. Registrar el voto en la colección registro_votos (Mongo)
    registro_votos_collection.insert_one({
        "id_concursante": concursante_id,
        "fecha_hora": datetime.now()
    })

    # 5. Actualizar Redis
    # Clave por concursante: votos:<id>
    redis_key = f"votos:{concursante_id}"
    redis_client.incr(redis_key)          # si no existe, Redis la crea con 1 (B3)
    redis_client.incr("votos_total")      # contador global de votos (opcional, pero útil para el panel)

    # 6. Marcar en la sesión que ya votó por este concursante
    votos_emitidos.append(concursante_id)
    session["votos_emitidos"] = votos_emitidos

    flash("¡Voto registrado correctamente!")
    return redirect(url_for("mostrar_concursantes"))

# ================= MÓDULO ADMIN =================

@app.route("/admin")
def admin_panel():
    """
    Pantalla principal del administrador.
    Desde aquí se puede:
    - Cargar concursantes desde un archivo JSON.
    - Ir al formulario para agregar nuevo participante.
    """
    return render_template("admin_panel.html")


@app.route("/admin/cargar", methods=["POST"])
@requiere_rol("admin")
def cargar_concursantes():
    """
    Carga los concursantes desde un archivo JSON subido por el administrador.
    - El admin selecciona el archivo en el navegador.
    - Por cada concursante del JSON:
        * Si ya existe (mismo id), se actualiza su info básica.
        * Si no existe, se inserta como nuevo.
    - Los participantes agregados manualmente (que no estén en el JSON) se mantienen.
    """
    archivo = request.files.get("archivo_json")

    if not archivo or archivo.filename == "":
        flash("Debe seleccionar un archivo .json.")
        return redirect(url_for("admin_panel"))

    try:
        concursantes = json.load(archivo)

        nuevos = 0
        actualizados = 0

        for c in concursantes:
            # Buscamos si ya existe un concursante con ese id
            existente = concursantes_collection.find_one({"id": c["id"]})

            if existente:
                # Opcional: conservar votos acumulados que ya tenía en la BD
                c["votos_acumulados"] = existente.get("votos_acumulados", 0)

                concursantes_collection.update_one(
                    {"id": c["id"]},
                    {"$set": {
                        "nombre": c["nombre"],
                        "categoria": c["categoria"],
                        "foto": c["foto"],
                        "votos_acumulados": c.get("votos_acumulados", 0)
                    }}
                )
                actualizados += 1
            else:
                # No existía, lo insertamos como nuevo
                concursantes_collection.insert_one(c)
                nuevos += 1

        flash(
            f"Concursantes cargados correctamente. {nuevos} nuevos, {actualizados} actualizados. "
            "Los participantes agregados manualmente se mantienen."
        )
    except Exception as e:
        flash(f"Error al cargar concursantes: {e}")

    return redirect(url_for("admin_panel"))

@app.route("/admin/agregar", methods=["GET"])
@requiere_rol("admin")
def form_agregar_participante():
    """
    Muestra el formulario para agregar un nuevo participante.
    """
    return render_template("admin_agregar.html")


@app.route("/admin/agregar", methods=["POST"])
@requiere_rol("admin")
def agregar_participante():
    """
    Recibe los datos del formulario y crea un nuevo participante.
    - Sube la foto seleccionada y la guarda en static/fotos.
    - Calcula un id nuevo (max(id) + 1).
    - Asigna votos_acumulados = 0.
    """
    nombre = request.form.get("nombre")
    categoria = request.form.get("categoria")
    archivo_foto = request.files.get("foto")

    if not nombre or not categoria or not archivo_foto or archivo_foto.filename == "":
        flash("Todos los campos (incluida la foto) son obligatorios.")
        return redirect(url_for("form_agregar_participante"))

    # Validar extensión del archivo
    if not allowed_file(archivo_foto.filename):
        flash("El archivo de foto debe ser una imagen (png, jpg, jpeg, gif).")
        return redirect(url_for("form_agregar_participante"))

    # Guardar la foto en static/fotos con un nombre seguro
    filename = secure_filename(archivo_foto.filename)
    ruta_guardado = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    archivo_foto.save(ruta_guardado)

    # Calcular un id nuevo: tomar el máximo id actual y sumarle 1
    ultimo = concursantes_collection.find_one(sort=[("id", -1)])
    if ultimo is None:
        nuevo_id = 1
    else:
        nuevo_id = ultimo["id"] + 1

    nuevo_concursante = {
        "id": nuevo_id,
        "nombre": nombre,
        "categoria": categoria,
        "foto": filename,          # se guarda el nombre del archivo
        "votos_acumulados": 0
    }

    try:
        concursantes_collection.insert_one(nuevo_concursante)
        flash(f"Participante '{nombre}' agregado correctamente con id {nuevo_id}.")
        return redirect(url_for("admin_panel"))
    except Exception as e:
        flash(f"Error al agregar participante: {e}")
        return redirect(url_for("form_agregar_participante"))

@app.route("/admin/monitor")
@requiere_rol("admin")
def admin_monitor():
    """
    Muestra un panel simple de votación en tiempo real usando Redis.
    - Total de votos.
    - Votos por concursante.
    """
    # Traer concursantes desde Mongo
    concursantes = list(concursantes_collection.find({}, {"_id": 0}))

    datos = []
    for c in concursantes:
        clave = f"votos:{c['id']}"
        votos_redis = redis_client.get(clave)
        if votos_redis is None:
            votos_redis = 0
        else:
            votos_redis = int(votos_redis)

        datos.append({
            "id": c["id"],
            "nombre": c["nombre"],
            "categoria": c["categoria"],
            "foto": c["foto"],
            "votos_redis": votos_redis
        })

    total_votos = redis_client.get("votos_total")
    if total_votos is None:
        total_votos = 0
    else:
        total_votos = int(total_votos)

    return render_template(
        "admin_monitor.html",
        concursantes=datos,
        total_votos=total_votos
    )

if __name__ == "__main__":
    app.run(debug=True)
