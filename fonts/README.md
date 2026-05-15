# Шрифты

Файлы шрифтов сюда не положены намеренно. Бот сам ищет системные шрифты с кириллицей.

Рекомендуется поставить системные шрифты:

```bash
sudo apt update
sudo apt install fonts-dejavu fonts-noto fonts-noto-color-emoji
```

Или положи свой `.ttf/.otf` в любую папку и укажи путь в `.env`:

```env
FONT_PATH=/absolute/path/to/NotoSans-Bold.ttf
```
