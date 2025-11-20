from pymongo import MongoClient

def get_mongo_client():
    # Si tu Mongo está en otra dirección/puerto, cámbialo aquí
    client = MongoClient("mongodb://localhost:27017/")
    return client

def get_database():
    client = get_mongo_client()
    # Nombre de la base de datos para el proyecto
    return client["concurso_talentos"]
