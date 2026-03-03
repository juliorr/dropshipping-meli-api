# Dropshipping MeLi API

FastAPI service dedicated to MercadoLibre integration (OAuth, listings, categories, orders).

## Part of the project

This service is used as part of the main project [dropshipping-amazon-meli](https://github.com/juliorr/dropshipping-amazon-meli) as a git submodule in `services/meli-api/`.

## Stack

- Python 3.13 + FastAPI
- PostgreSQL 17 (asyncpg)
- APScheduler (periodic tasks)
- Alembic (migrations)

## Development

This service is launched from the main project's `docker-compose`. See instructions in the main repo.

## Contact

If you want to know how to use this project, contact me: juliorr@gmail.com
