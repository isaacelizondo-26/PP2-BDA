import json
from database.mongo_connection import get_database

db = get_database()
concursantes_collection = db["concursantes"]

with open("concursantes.json", "r", encoding="utf-8") as f:
    concursantes = json.load(f)

# Opcional: limpia la colección antes de cargar
concursantes_collection.delete_many({})

# Inserta todos los concursantes del JSON
concursantes_collection.insert_many(concursantes)

print("Concursantes cargados correctamente en MongoDB.")
