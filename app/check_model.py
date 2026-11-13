"""Run an actual, minimal configured-model request without printing credentials."""

if __name__ == '__main__':
    import argparse
    from pathlib import Path
    from dotenv import load_dotenv
    parser = argparse.ArgumentParser(description='Проверка реального запроса без вывода ключа')
    parser.add_argument('--env-file', help='Дополнительный локальный файл конфигурации, например .env.gemini')
    args = parser.parse_args()
    if args.env_file:
        path = Path(args.env_file)
        if not path.is_file():
            raise SystemExit('Файл конфигурации не найден.')
        load_dotenv(path, override=True)
    from app.services.llm import LLM, ModelError
    try:
        print(LLM().probe()['message'])
    except ModelError as exc:
        print(str(exc))
        raise SystemExit(1) from None
