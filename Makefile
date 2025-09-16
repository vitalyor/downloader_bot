SHELL := /bin/bash

up:
\tdocker compose up -d --build

down:
\tdocker compose down

logs:
\tdocker compose logs -f

ps:
\tdocker compose ps

pull:
\tgit pull --rebase

deploy: pull up logs