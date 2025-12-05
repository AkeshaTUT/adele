import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from models import Base
from config import DATABASE_URL


async def migrate_database():
    """Обновляет структуру базы данных без потери данных"""
    engine = create_async_engine(DATABASE_URL, echo=True)

    async with engine.begin() as conn:
        # Проверяем, существует ли колонка photo_file_id
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='menu_items' AND column_name='photo_file_id';
        """))

        column_exists = result.fetchone()

        if not column_exists:
            # Добавляем новую колонку для фотографий
            await conn.execute(text("""
                ALTER TABLE menu_items 
                ADD COLUMN photo_file_id VARCHAR;
            """))
            print("✅ Добавлена колонка photo_file_id в таблицу menu_items")
        else:
            print("ℹ️ Колонка photo_file_id уже существует")

        # Создаем все остальные таблицы, если их нет
        await conn.run_sync(Base.metadata.create_all)
        print("✅ Структура базы данных обновлена")


if __name__ == "__main__":
    print("🔄 Обновление структуры базы данных...")
    asyncio.run(migrate_database())
    print("✅ Миграция завершена!")
