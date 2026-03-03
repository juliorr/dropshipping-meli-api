# Dropshipping MeLi API

Servicio FastAPI dedicado a la integración con MercadoLibre (OAuth, publicaciones, categorías, órdenes).

## Parte del proyecto

Este servicio se levanta como parte del proyecto principal [dropshipping-amazon-meli](https://github.com/juliorr/dropshipping-amazon-meli) como git submodule en `services/meli-api/`.

## Stack

- Python 3.13 + FastAPI
- PostgreSQL 17 (asyncpg)
- APScheduler (tareas periódicas)
- Alembic (migraciones)

## Desarrollo

Este servicio se levanta desde el `docker-compose` del proyecto principal. Ver instrucciones en el repo principal.
