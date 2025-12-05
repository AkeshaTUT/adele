import asyncio
import logging
from typing import List
import os
from io import BytesIO

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import InputFile, ContentType

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import update, delete

from models import Base, User, MenuItem, Order, OrderItem
from config import DATABASE_URL, LOG_LEVEL, ADMIN_BOT_TOKEN, ALLOWED_ADMIN_IDS

# Инициализация
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
admin_bot = Bot(token=ADMIN_BOT_TOKEN)
storage = MemoryStorage()
admin_dp = Dispatcher(admin_bot, storage=storage)

# Асинхронный движок БД
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# FSM состояния для админ-бота
class AdminStates(StatesGroup):
    # Управление меню
    adding_item_category = State()
    adding_item_name = State()
    adding_item_price = State()
    adding_item_photo = State()

    editing_item_select = State()
    editing_item_field = State()
    editing_item_value = State()

    # Управление заказами
    viewing_orders = State()
    order_details = State()


# Проверка прав администратора
def is_admin(user_id: int) -> bool:
    return user_id in ALLOWED_ADMIN_IDS


# Админ клавиатуры
def get_admin_main_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("📋 Активные заказы"))
    keyboard.add(KeyboardButton("📊 Все заказы"), KeyboardButton("📈 Статистика"))
    keyboard.add(KeyboardButton("🍽 Управление меню"))
    keyboard.add(KeyboardButton("👥 Пользователи"), KeyboardButton("⚙️ Настройки"))
    return keyboard


def get_menu_management_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("➕ Добавить позицию", callback_data="add_menu_item"))
    keyboard.add(InlineKeyboardButton("✏️ Редактировать позицию", callback_data="edit_menu_item"))
    keyboard.add(InlineKeyboardButton("🗑 Удалить позицию", callback_data="delete_menu_item"))
    keyboard.add(InlineKeyboardButton("📂 Управление категориями", callback_data="manage_categories"))
    keyboard.add(InlineKeyboardButton("📸 Добавить фотографии", callback_data="add_photos"))
    return keyboard


def get_order_action_keyboard(order_id: int):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Готов", callback_data=f"ready:{order_id}"))
    keyboard.add(InlineKeyboardButton("❌ Отменить", callback_data=f"cancel:{order_id}"))
    keyboard.add(InlineKeyboardButton("📞 Связаться", callback_data=f"contact:{order_id}"))
    keyboard.add(InlineKeyboardButton("⏰ Изменить время", callback_data=f"time:{order_id}"))
    return keyboard


# Функции работы с БД для админа
async def get_pending_orders():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Order).options(
                selectinload(Order.user),
                selectinload(Order.order_items).selectinload(OrderItem.menu_item)
            ).where(Order.status == 'pending').order_by(Order.pickup_time)
        )
        return result.scalars().all()


async def get_all_orders(limit: int = 20):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Order).options(
                selectinload(Order.user),
                selectinload(Order.order_items).selectinload(OrderItem.menu_item)
            ).order_by(Order.created_at.desc()).limit(limit)
        )
        return result.scalars().all()


async def get_order_statistics():
    async with AsyncSessionLocal() as session:
        # Общее количество заказов
        total_orders = await session.execute(select(Order))
        total_count = len(total_orders.scalars().all())

        # Количество активных заказов
        pending_orders = await session.execute(
            select(Order).where(Order.status == 'pending')
        )
        pending_count = len(pending_orders.scalars().all())

        # Количество готовых заказов
        ready_orders = await session.execute(
            select(Order).where(Order.status == 'ready')
        )
        ready_count = len(ready_orders.scalars().all())

        # Общая сумма заказов
        all_orders = await session.execute(
            select(Order).options(
                selectinload(Order.order_items).selectinload(OrderItem.menu_item)
            )
        )
        orders = all_orders.scalars().all()
        total_revenue = 0
        for order in orders:
            for item in order.order_items:
                total_revenue += item.menu_item.price * item.quantity

        return {
            'total_orders': total_count,
            'pending_orders': pending_count,
            'ready_orders': ready_count,
            'total_revenue': total_revenue
        }


async def format_order_for_admin(order: Order) -> str:
    message = f"🆔 Заказ #{order.id}\n"
    message += f"👤 Пользователь: @{order.user.username or 'неизвестно'} (ID: {order.user.telegram_id})\n"
    message += f"📅 Создан: {order.created_at.strftime('%d.%m.%Y %H:%M')}\n"
    message += f"🕐 Время получения: {order.pickup_time.strftime('%d.%m.%Y %H:%M')}\n"
    message += f"📊 Статус: {'⏳ Ожидает' if order.status == 'pending' else '✅ Готов'}\n\n"

    message += "📝 Состав заказа:\n"
    total_price = 0
    for order_item in order.order_items:
        item_total = order_item.menu_item.price * order_item.quantity
        total_price += item_total
        message += f"• {order_item.menu_item.name} x{order_item.quantity} = {item_total}₸\n"

    message += f"\n💰 Итого: {total_price}₸"
    return message


# Админ обработчики
@admin_dp.message_handler(commands=['start'])
async def admin_start(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для использования этого бота.")
        return

    await message.answer(
        "👨‍💼 Добро пожаловать в админ-панель кафе!\n\n"
        "Здесь вы можете:\n"
        "📋 Управлять заказами\n"
        "🍽 Редактировать меню\n"
        "📊 Просматривать статистику\n"
        "👥 Управлять пользователями",
        reply_markup=get_admin_main_keyboard()
    )


@admin_dp.message_handler(lambda message: message.text == "📋 Активные заказы")
async def show_active_orders(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    orders = await get_pending_orders()

    if not orders:
        await message.answer("📋 Нет активных заказов")
        return

    await message.answer(f"📋 Активных заказов: {len(orders)}")

    for order in orders:
        order_text = await format_order_for_admin(order)
        keyboard = get_order_action_keyboard(order.id)
        await message.answer(order_text, reply_markup=keyboard)


@admin_dp.message_handler(lambda message: message.text == "📊 Все заказы")
async def show_all_orders(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    orders = await get_all_orders()

    if not orders:
        await message.answer("📊 Заказов пока нет")
        return

    orders_text = f"📊 Последние {len(orders)} заказов:\n\n"

    for order in orders:
        status_emoji = "⏳" if order.status == "pending" else "✅"
        total_price = sum(item.menu_item.price * item.quantity for item in order.order_items)

        orders_text += f"{status_emoji} Заказ #{order.id}\n"
        orders_text += f"👤 @{order.user.username or 'неизвестно'}\n"
        orders_text += f"💰 {total_price}₸ | 🕐 {order.pickup_time.strftime('%H:%M')}\n\n"

    await message.answer(orders_text)


@admin_dp.message_handler(lambda message: message.text == "📈 Статистика")
async def show_statistics(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    stats = await get_order_statistics()

    stats_text = "📈 Статистика кафе:\n\n"
    stats_text += f"📊 Всего заказов: {stats['total_orders']}\n"
    stats_text += f"⏳ Активных заказов: {stats['pending_orders']}\n"
    stats_text += f"✅ Готовых заказов: {stats['ready_orders']}\n"
    stats_text += f"💰 Общая выручка: {stats['total_revenue']:,.0f}₸\n"

    if stats['total_orders'] > 0:
        avg_order = stats['total_revenue'] / stats['total_orders']
        stats_text += f"📊 Средний чек: {avg_order:,.0f}₸"

    await message.answer(stats_text)


@admin_dp.message_handler(lambda message: message.text == "🍽 Управление меню")
async def manage_menu(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    keyboard = get_menu_management_keyboard()
    await message.answer(
        "🍽 Управление меню:\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('ready:'))
async def mark_order_ready(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    order_id = int(callback_query.data.split(':')[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Order).options(selectinload(Order.user)).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()

        if order:
            order.status = 'ready'
            await session.commit()

            # Уведомляем пользователя через основной бот
            from config import BOT_TOKEN
            client_bot = Bot(token=BOT_TOKEN)
            try:
                await client_bot.send_message(
                    order.user.telegram_id,
                    f"✅ Ваш заказ №{order.id} готов к выдаче!\n"
                    f"🕐 Время получения: {order.pickup_time.strftime('%H:%M')}"
                )
                await client_bot.session.close()
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления пользователю: {e}")

            await callback_query.message.edit_text(
                callback_query.message.text + "\n\n✅ Заказ отмечен как готовый"
            )
        else:
            await callback_query.answer("Заказ не найден")


@admin_dp.callback_query_handler(lambda c: c.data == "add_menu_item")
async def start_adding_item(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    await callback_query.message.answer("📝 Введите категорию нового блюда:")
    await AdminStates.adding_item_category.set()


@admin_dp.message_handler(state=AdminStates.adding_item_category)
async def process_item_category(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(category=message.text)
    await message.answer("📝 Введите название блюда:")
    await AdminStates.adding_item_name.set()


@admin_dp.message_handler(state=AdminStates.adding_item_name)
async def process_item_name(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(name=message.text)
    await message.answer("💰 Введите цену блюда (в тенге):")
    await AdminStates.adding_item_price.set()


@admin_dp.message_handler(state=AdminStates.adding_item_price)
async def process_item_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        price = float(message.text)
        await state.update_data(price=price)

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("📸 Добавить фото", callback_data="add_photo"))
        keyboard.add(InlineKeyboardButton("✅ Сохранить без фото", callback_data="save_without_photo"))

        await message.answer(
            "Хотите добавить фотографию для этого блюда?",
            reply_markup=keyboard
        )
        await AdminStates.adding_item_photo.set()

    except ValueError:
        await message.answer("❌ Неверный формат цены. Введите число:")


@admin_dp.callback_query_handler(lambda c: c.data == "save_without_photo", state=AdminStates.adding_item_photo)
async def save_item_without_photo(callback_query: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback_query.from_user.id):
        return

    data = await state.get_data()

    async with AsyncSessionLocal() as session:
        new_item = MenuItem(
            category=data['category'],
            name=data['name'],
            price=data['price'],
            is_available=True
        )
        session.add(new_item)
        await session.commit()

    await callback_query.message.edit_text(
        f"✅ Новое блюдо добавлено:\n"
        f"📂 Категория: {data['category']}\n"
        f"🍽 Название: {data['name']}\n"
        f"💰 Цена: {data['price']}₸"
    )

    await state.finish()


@admin_dp.message_handler(content_types=ContentType.PHOTO, state=AdminStates.adding_item_photo)
async def process_photo_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    photo_file_id = message.photo[-1].file_id

    # Проверяем, добавляем ли фото к существующей позиции или создаем новую
    if 'adding_photo_to_item_id' in data:
        # Добавляем фото к существующей позиции
        item_id = data['adding_photo_to_item_id']

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
            item = result.scalar_one_or_none()

            if item:
                item.photo_file_id = photo_file_id
                await session.commit()
                await message.answer(f"✅ Фото добавлено для '{item.name}'!")
            else:
                await message.answer("❌ Позиция не найдена")
    else:
        # Создаем новую позицию с фото
        async with AsyncSessionLocal() as session:
            new_item = MenuItem(
                category=data['category'],
                name=data['name'],
                price=data['price'],
                is_available=True,
                photo_file_id=photo_file_id
            )
            session.add(new_item)
            await session.commit()

        await message.answer(
            f"✅ Новое блюдо с фото добавлено:\n"
            f"📂 Категория: {data['category']}\n"
            f"🍽 Название: {data['name']}\n"
            f"💰 Цена: {data['price']}₸"
        )

    await state.finish()


@admin_dp.callback_query_handler(lambda c: c.data == "add_photo", state=AdminStates.adding_item_photo)
async def request_photo_for_new_item(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    await callback_query.message.answer("📸 Отправьте фотографию нового блюда:")
    # Состояние уже установлено, просто ждем фото


@admin_dp.callback_query_handler(lambda c: c.data == "edit_menu_item")
async def start_editing_item(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem).where(MenuItem.is_available == True))
        items = result.scalars().all()

    if not items:
        await callback_query.message.answer("❌ В меню нет позиций для редактирования")
        return

    keyboard = InlineKeyboardMarkup()
    for item in items:
        keyboard.add(InlineKeyboardButton(
            f"{item.name} - {item.price}₸",
            callback_data=f"edit_item:{item.id}"
        ))

    await callback_query.message.answer(
        "✏️ Выберите позицию для редактирования:",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('edit_item:'))
async def select_edit_field(callback_query: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback_query.from_user.id):
        return

    item_id = int(callback_query.data.split(':')[1])
    await state.update_data(editing_item_id=item_id)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
        item = result.scalar_one_or_none()

    if not item:
        await callback_query.message.answer("❌ Позиция не найдена")
        return

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📂 Категория", callback_data="edit_field:category"))
    keyboard.add(InlineKeyboardButton("🍽 Название", callback_data="edit_field:name"))
    keyboard.add(InlineKeyboardButton("💰 Цена", callback_data="edit_field:price"))
    keyboard.add(InlineKeyboardButton("📸 Фото", callback_data="edit_field:photo"))
    keyboard.add(InlineKeyboardButton("🔄 Доступность", callback_data="edit_field:availability"))

    await callback_query.message.edit_text(
        f"✏️ Редактирование: {item.name}\n\n"
        f"📂 Категория: {item.category}\n"
        f"🍽 Название: {item.name}\n"
        f"💰 Цена: {item.price}₸\n"
        f"📸 Фото: {'Есть' if item.photo_file_id else 'Нет'}\n"
        f"🔄 Доступность: {'Да' if item.is_available else 'Нет'}\n\n"
        "Что хотите изменить?",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('edit_field:'))
async def process_edit_field(callback_query: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback_query.from_user.id):
        return

    field = callback_query.data.split(':')[1]
    await state.update_data(editing_field=field)

    if field == "category":
        await callback_query.message.answer("📂 Введите новую категорию:")
        await AdminStates.editing_item_value.set()
    elif field == "name":
        await callback_query.message.answer("🍽 Введите новое название:")
        await AdminStates.editing_item_value.set()
    elif field == "price":
        await callback_query.message.answer("💰 Введите новую цену (в тенге):")
        await AdminStates.editing_item_value.set()
    elif field == "photo":
        await callback_query.message.answer("📸 Отправьте новую фотографию:")
        await AdminStates.editing_item_value.set()
    elif field == "availability":
        data = await state.get_data()
        item_id = data['editing_item_id']

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
            item = result.scalar_one_or_none()

            if item:
                item.is_available = not item.is_available
                await session.commit()

                status = "доступна" if item.is_available else "недоступна"
                await callback_query.message.answer(f"✅ Позиция '{item.name}' теперь {status}")

        await state.finish()


@admin_dp.message_handler(state=AdminStates.editing_item_value)
async def update_item_field(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    item_id = data['editing_item_id']
    field = data['editing_field']

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
        item = result.scalar_one_or_none()

        if not item:
            await message.answer("❌ Позиция не найдена")
            await state.finish()
            return

        try:
            if field == "category":
                item.category = message.text
            elif field == "name":
                item.name = message.text
            elif field == "price":
                item.price = float(message.text)

            await session.commit()
            await message.answer(f"✅ {field.capitalize()} обновлено успешно!")

        except ValueError:
            await message.answer("❌ Неверный формат данных. Попробуйте снова:")
            return

    await state.finish()


@admin_dp.message_handler(content_types=ContentType.PHOTO, state=AdminStates.editing_item_value)
async def update_item_photo(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    item_id = data['editing_item_id']

    photo_file_id = message.photo[-1].file_id

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
        item = result.scalar_one_or_none()

        if item:
            item.photo_file_id = photo_file_id
            await session.commit()
            await message.answer(f"✅ Фото для '{item.name}' обновлено!")

    await state.finish()


@admin_dp.callback_query_handler(lambda c: c.data == "delete_menu_item")
async def start_deleting_item(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem))
        items = result.scalars().all()

    if not items:
        await callback_query.message.answer("❌ В меню нет позиций для удаления")
        return

    keyboard = InlineKeyboardMarkup()
    for item in items:
        status = "✅" if item.is_available else "❌"
        keyboard.add(InlineKeyboardButton(
            f"{status} {item.name} - {item.price}₸",
            callback_data=f"delete_item:{item.id}"
        ))

    await callback_query.message.answer(
        "🗑 Выберите позицию для удаления:",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('delete_item:'))
async def confirm_delete_item(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    item_id = int(callback_query.data.split(':')[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
        item = result.scalar_one_or_none()

        if not item:
            await callback_query.message.answer("❌ Позиция не найдена")
            return

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete:{item_id}"))
        keyboard.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete"))

        await callback_query.message.edit_text(
            f"⚠️ Вы уверены, что хотите удалить:\n\n"
            f"🍽 {item.name}\n"
            f"📂 {item.category}\n"
            f"💰 {item.price}₸\n\n"
            f"❗ Это действие нельзя отменить!",
            reply_markup=keyboard
        )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('confirm_delete:'))
async def delete_item_confirmed(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    item_id = int(callback_query.data.split(':')[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
        item = result.scalar_one_or_none()

        if item:
            item_name = item.name
            await session.delete(item)
            await session.commit()

            await callback_query.message.edit_text(
                f"✅ Позиция '{item_name}' успешно удалена из меню"
            )
        else:
            await callback_query.message.edit_text("❌ Позиция не найдена")


@admin_dp.callback_query_handler(lambda c: c.data == "cancel_delete")
async def cancel_delete(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("❌ Удаление отменено")


@admin_dp.callback_query_handler(lambda c: c.data == "manage_categories")
async def manage_categories(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem.category).distinct())
        categories = result.scalars().all()

        categories_text = "📂 Категории в меню:\n\n"
        for i, category in enumerate(categories, 1):
            # Подсчитываем количество позиций в категории
            count_result = await session.execute(
                select(MenuItem).where(MenuItem.category == category)
            )
            count = len(count_result.scalars().all())
            categories_text += f"{i}. {category} ({count} позиций)\n"

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("➕ Добавить категорию", callback_data="add_category"))
    keyboard.add(InlineKeyboardButton("🗑 Удалить категорию", callback_data="delete_category"))

    await callback_query.message.answer(categories_text, reply_markup=keyboard)


@admin_dp.callback_query_handler(lambda c: c.data == "add_photos")
async def add_photos_menu(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        # Получаем все позиции без фото
        result = await session.execute(
            select(MenuItem).where(MenuItem.photo_file_id.is_(None))
        )
        items_without_photos = result.scalars().all()

    if not items_without_photos:
        await callback_query.message.answer("✅ У всех позиций уже есть фотографии!")
        return

    # Группируем по категориям
    categories_with_items = {}
    for item in items_without_photos:
        if item.category not in categories_with_items:
            categories_with_items[item.category] = []
        categories_with_items[item.category].append(item)

    # Создаем клавиатуру с категориями
    keyboard = InlineKeyboardMarkup()
    for category, items in categories_with_items.items():
        keyboard.add(InlineKeyboardButton(
            f"📂 {category} ({len(items)} позиций)",
            callback_data=f"photo_category:{category}"
        ))

    keyboard.add(InlineKeyboardButton("📸 Показать все позиции", callback_data="photo_all_items"))

    await callback_query.message.answer(
        f"📸 Позиций без фотографий: {len(items_without_photos)}\n\n"
        "Выберите категорию:",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('photo_category:'))
async def show_items_by_category_for_photo(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    category = callback_query.data.split(':', 1)[1]

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MenuItem).where(
                MenuItem.photo_file_id.is_(None),
                MenuItem.category == category
            )
        )
        items_without_photos = result.scalars().all()

    if not items_without_photos:
        await callback_query.message.edit_text("✅ В этой категории у всех позиций уже есть фотографии!")
        return

    keyboard = InlineKeyboardMarkup()
    for item in items_without_photos:
        keyboard.add(InlineKeyboardButton(
            f"📸 {item.name} - {item.price}₸",
            callback_data=f"add_photo_to:{item.id}"
        ))

    keyboard.add(InlineKeyboardButton("⬅️ Назад к категориям", callback_data="add_photos"))

    await callback_query.message.edit_text(
        f"📂 Категория: {category}\n"
        f"📸 Позиций без фото: {len(items_without_photos)}\n\n"
        "Выберите позицию для добавления фото:",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data == "photo_all_items")
async def show_all_items_for_photo(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MenuItem).where(MenuItem.photo_file_id.is_(None))
        )
        items_without_photos = result.scalars().all()

    if not items_without_photos:
        await callback_query.message.edit_text("✅ У всех позиций уже есть фотографии!")
        return

    keyboard = InlineKeyboardMarkup()
    for item in items_without_photos:
        keyboard.add(InlineKeyboardButton(
            f"📸 {item.name} ({item.category}) - {item.price}₸",
            callback_data=f"add_photo_to:{item.id}"
        ))

    keyboard.add(InlineKeyboardButton("⬅️ Назад к категориям", callback_data="add_photos"))

    await callback_query.message.edit_text(
        f"📸 Все позиции без фотографий ({len(items_without_photos)}):\n"
        "Выберите позицию для добавления фото:",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('add_photo_to:'))
async def start_adding_photo(callback_query: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback_query.from_user.id):
        return

    item_id = int(callback_query.data.split(':')[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
        item = result.scalar_one_or_none()

    if not item:
        await callback_query.message.answer("❌ Позиция не найдена")
        return

    await state.update_data(adding_photo_to_item_id=item_id)
    await callback_query.message.answer(f"📸 Отправьте фотографию для '{item.name}':")
    await AdminStates.adding_item_photo.set()


@admin_dp.callback_query_handler(lambda c: c.data.startswith('cancel:'))
async def cancel_order(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    order_id = int(callback_query.data.split(':')[1])

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Да, отменить заказ", callback_data=f"confirm_cancel:{order_id}"))
    keyboard.add(InlineKeyboardButton("❌ Нет, вернуться", callback_data="cancel_action"))

    await callback_query.message.edit_text(
        f"⚠️ Вы уверены, что хотите отменить заказ #{order_id}?\n"
        "Пользователь получит уведомление об отмене.",
        reply_markup=keyboard
    )


@admin_dp.callback_query_handler(lambda c: c.data.startswith('confirm_cancel:'))
async def confirm_cancel_order(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        return

    order_id = int(callback_query.data.split(':')[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Order).options(selectinload(Order.user)).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()

        if order:
            order.status = 'cancelled'
            await session.commit()

            # Уведомляем пользователя
            from main import bot
            try:
                await bot.send_message(
                    order.user.telegram_id,
                    f"❌ Ваш заказ №{order.id} был отменен администратором.\n"
                    f"Если у вас есть вопросы, обратитесь к администрации кафе."
                )
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления об отмене: {e}")

            await callback_query.message.edit_text(
                callback_query.message.text + "\n\n❌ Заказ отменен"
            )
        else:
            await callback_query.answer("Заказ не найден")


@admin_dp.callback_query_handler(lambda c: c.data == "cancel_action")
async def cancel_action(callback_query: types.CallbackQuery):
    await callback_query.message.delete()


# Обработчик для добавления фото к существующим позициям
@admin_dp.message_handler(content_types=ContentType.PHOTO, state=AdminStates.adding_item_photo)
async def process_photo_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    photo_file_id = message.photo[-1].file_id

    # Проверяем, добавляем ли фото к существующей позиции или создаем новую
    if 'adding_photo_to_item_id' in data:
        # Добавляем фото к существующей позиции
        item_id = data['adding_photo_to_item_id']

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(MenuItem).where(MenuItem.id == item_id))
            item = result.scalar_one_or_none()

            if item:
                item.photo_file_id = photo_file_id
                await session.commit()
                await message.answer(f"✅ Фото добавлено для '{item.name}'!")
            else:
                await message.answer("❌ Позиция не найдена")
    else:
        # Создаем новую позицию с фото
        async with AsyncSessionLocal() as session:
            new_item = MenuItem(
                category=data['category'],
                name=data['name'],
                price=data['price'],
                is_available=True,
                photo_file_id=photo_file_id
            )
            session.add(new_item)
            await session.commit()

        await message.answer(
            f"✅ Новое блюдо с фото добавлено:\n"
            f"📂 Категория: {data['category']}\n"
            f"🍽 Название: {data['name']}\n"
            f"💰 Цена: {data['price']}₸"
        )

    await state.finish()


async def main():
    # Создаем таблицы при запуске
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("🔧 Админ-бот запущен!")
    print(f"👨‍💼 Разрешенные администраторы: {ALLOWED_ADMIN_IDS}")
    await admin_dp.start_polling()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Админ-бот остановлен пользователем")
    except Exception as e:
        print(f"❌ Ошибка запуска админ-бота: {e}")
        import traceback
        traceback.print_exc()
