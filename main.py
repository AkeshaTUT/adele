import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from models import Base, User, MenuItem, Order, OrderItem
from config import BOT_TOKEN, DATABASE_URL, ADMIN_CHAT_ID, LOG_LEVEL

# Инициализация
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Асинхронный движок БД
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# FSM состояния
class OrderStates(StatesGroup):
    choosing_category = State()
    choosing_item = State()
    choosing_quantity = State()
    choosing_time = State()
    confirmation = State()


# Клавиатуры
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("🍽 Меню"))
    keyboard.add(KeyboardButton("🛒 Мой заказ"), KeyboardButton("👤 Мои заказы"))
    return keyboard


async def get_categories_keyboard():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MenuItem.category).distinct().where(MenuItem.is_available == True)
        )
        categories = result.scalars().all()

    keyboard = InlineKeyboardMarkup()
    for category in categories:
        keyboard.add(InlineKeyboardButton(category, callback_data=f"category:{category}"))
    return keyboard


async def get_menu_items_keyboard(category: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MenuItem).where(
                MenuItem.category == category,
                MenuItem.is_available == True
            )
        )
        items = result.scalars().all()

    keyboard = InlineKeyboardMarkup()
    for item in items:
        keyboard.add(InlineKeyboardButton(
            f"{item.name} - {item.price}₸",
            callback_data=f"add:{item.id}"
        ))
    keyboard.add(InlineKeyboardButton("⬅️ Назад к категориям", callback_data="back_to_categories"))
    return keyboard, items  # Возвращаем также список items для отображения фотографий


def get_cart_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Оформить заказ", callback_data="checkout"))
    keyboard.add(InlineKeyboardButton("🗑 Очистить корзину", callback_data="clear_cart"))
    keyboard.add(InlineKeyboardButton("🍽 Продолжить покупки", callback_data="continue_shopping"))
    return keyboard


# Функции работы с БД
async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(telegram_id: int, username: str = None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        return user


async def get_menu_item(item_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MenuItem).where(MenuItem.id == item_id)
        )
        return result.scalar_one_or_none()


async def save_order(user_id: int, cart: Dict[str, int], pickup_time: datetime):
    async with AsyncSessionLocal() as session:
        # Создаем заказ
        order = Order(
            user_id=user_id,
            pickup_time=pickup_time,
            status='pending'
        )
        session.add(order)
        await session.flush()  # Получаем ID заказа

        # Добавляем позиции заказа
        for item_id, quantity in cart.items():
            order_item = OrderItem(
                order_id=order.id,
                menu_item_id=int(item_id),
                quantity=quantity
            )
            session.add(order_item)

        await session.commit()
        await session.refresh(order)
        return order


async def format_cart_message(cart: Dict[str, int]):
    if not cart:
        return "🛒 Ваша корзина пуста"

    total_price = 0
    cart_text = "🛒 Ваша корзина:\n\n"

    async with AsyncSessionLocal() as session:
        for item_id, quantity in cart.items():
            result = await session.execute(
                select(MenuItem).where(MenuItem.id == int(item_id))
            )
            item = result.scalar_one_or_none()
            if item:
                item_total = item.price * quantity
                total_price += item_total
                cart_text += f"• {item.name} x{quantity} = {item_total}₸\n"

    cart_text += f"\n💰 Итого: {total_price}₸"
    return cart_text


async def notify_admin_about_order(order_id: int):
    # Создаем отдельный экземпляр админ-бота для отправки уведомлений
    from config import ADMIN_BOT_TOKEN, ALLOWED_ADMIN_IDS
    admin_notification_bot = Bot(token=ADMIN_BOT_TOKEN)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Order).options(
                selectinload(Order.user),
                selectinload(Order.order_items).selectinload(OrderItem.menu_item)
            ).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()

        if not order:
            return

        message = f"🆕 Новый заказ #{order.id}\n\n"
        message += f"👤 Пользователь: @{order.user.username or 'неизвестно'}\n"
        message += f"🕐 Время получения: {order.pickup_time.strftime('%H:%M')}\n\n"
        message += "📝 Состав заказа:\n"

        total_price = 0
        for order_item in order.order_items:
            item_total = order_item.menu_item.price * order_item.quantity
            total_price += item_total
            message += f"• {order_item.menu_item.name} x{order_item.quantity} = {item_total}₸\n"

        message += f"\n💰 Итого: {total_price}₸"

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton(
            "✅ Заказ готов",
            callback_data=f"ready:{order.id}"
        ))

        # Отправляем уведомление всем администраторам
        for admin_id in ALLOWED_ADMIN_IDS:
            try:
                await admin_notification_bot.send_message(admin_id, message, reply_markup=keyboard)
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления админу {admin_id}: {e}")

        # Закрываем соединение
        await admin_notification_bot.session.close()


# Обработчики
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    user = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username
    )

    await message.answer(
        f"Добро пожаловать в систему предзаказа еды! 🍽\n\n"
        f"Выберите действие:",
        reply_markup=get_main_keyboard()
    )


@dp.message_handler(lambda message: message.text == "🍽 Меню")
async def show_menu(message: types.Message, state: FSMContext):
    keyboard = await get_categories_keyboard()
    await message.answer("Выберите категорию:", reply_markup=keyboard)
    await OrderStates.choosing_category.set()


@dp.callback_query_handler(lambda c: c.data.startswith('category:'), state=OrderStates.choosing_category)
async def process_category_selection(callback_query: types.CallbackQuery, state: FSMContext):
    category = callback_query.data.split(':')[1]
    keyboard, items = await get_menu_items_keyboard(category)

    # Если есть блюда с фотографиями, показываем их
    items_with_photos = [item for item in items if item.photo_file_id]

    if items_with_photos:
        # Показываем первое блюдо с фото
        first_item = items_with_photos[0]
        try:
            await callback_query.message.delete()
            await bot.send_photo(
                callback_query.from_user.id,
                first_item.photo_file_id,
                caption=f"Категория: {category}\nВыберите блюдо:",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Ошибка отправки фото: {e}")
            # Если фото не загружается или сообщение уже удалено, отправляем новое текстовое сообщение
            try:
                await bot.send_message(
                    callback_query.from_user.id,
                    f"Категория: {category}\nВыберите блюдо:",
                    reply_markup=keyboard
                )
            except Exception as e2:
                logging.error(f"Ошибка отправки сообщения: {e2}")
    else:
        try:
            await callback_query.message.edit_text(
                f"Категория: {category}\nВыберите блюдо:",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Ошибка редактирования сообщения: {e}")
            # Если сообщение нельзя отредактировать, отправляем новое
            await bot.send_message(
                callback_query.from_user.id,
                f"Категория: {category}\nВыберите блюдо:",
                reply_markup=keyboard
            )

    await OrderStates.choosing_item.set()
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data == "back_to_categories", state="*")
async def back_to_categories(callback_query: types.CallbackQuery, state: FSMContext):
    keyboard = await get_categories_keyboard()
    try:
        await callback_query.message.edit_text("Выберите категорию:", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка редактирования сообщения: {e}")
        # Если сообщение нельзя отредактировать, отправляем новое
        await bot.send_message(
            callback_query.from_user.id,
            "Выберите категорию:",
            reply_markup=keyboard
        )
    await OrderStates.choosing_category.set()
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data.startswith('add:'), state="*")
async def add_item_to_cart(callback_query: types.CallbackQuery, state: FSMContext):
    item_id = callback_query.data.split(':')[1]

    # Получаем текущую корзину
    data = await state.get_data()
    cart = data.get('cart', {})

    # Добавляем товар в корзину
    if item_id in cart:
        cart[item_id] += 1
    else:
        cart[item_id] = 1

    await state.update_data(cart=cart)

    # Получаем информацию о товаре
    item = await get_menu_item(int(item_id))

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🛒 Перейти в корзину", callback_data="show_cart"))
    keyboard.add(InlineKeyboardButton("🍽 Продолжить покупки", callback_data="continue_shopping"))

    success_message = f"✅ {item.name} добавлен в корзину!\n\nЧто делаем дальше?"

    # Если у товара есть фото, показываем его
    if item.photo_file_id:
        try:
            await callback_query.message.delete()
            await bot.send_photo(
                callback_query.from_user.id,
                item.photo_file_id,
                caption=success_message,
                reply_markup=keyboard
            )
        except Exception as e:
            # Если фото не загружается или сообщение уже удалено, отправляем новое сообщение
            logging.error(f"Ошибка отправки фото: {e}")
            try:
                await bot.send_message(
                    callback_query.from_user.id,
                    success_message,
                    reply_markup=keyboard
                )
            except Exception as e2:
                logging.error(f"Ошибка отправки сообщения: {e2}")
    else:
        try:
            await callback_query.message.edit_text(success_message, reply_markup=keyboard)
        except Exception as e:
            logging.error(f"Ошибка редактирования сообщения: {e}")
            # Если сообщение нельзя отредактировать, отправляем новое
            await bot.send_message(
                callback_query.from_user.id,
                success_message,
                reply_markup=keyboard
            )

    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data == "continue_shopping", state="*")
async def continue_shopping(callback_query: types.CallbackQuery, state: FSMContext):
    keyboard = await get_categories_keyboard()
    try:
        await callback_query.message.edit_text("Выберите категорию:", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка редактирования сообщения: {e}")
        # Если сообщение нельзя отредактировать, отправляем новое
        await bot.send_message(
            callback_query.from_user.id,
            "Выберите категорию:",
            reply_markup=keyboard
        )
    await OrderStates.choosing_category.set()
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data == "show_cart", state="*")
async def show_cart_callback(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get('cart', {})

    cart_message = await format_cart_message(cart)
    keyboard = get_cart_keyboard()

    try:
        await callback_query.message.edit_text(cart_message, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка редактирования сообщения: {e}")
        # Если сообщение нельзя отредактировать, отправляем новое
        await bot.send_message(
            callback_query.from_user.id,
            cart_message,
            reply_markup=keyboard
        )
    await callback_query.answer()


@dp.message_handler(lambda message: message.text == "🛒 Мой заказ")
async def show_cart(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get('cart', {})

    cart_message = await format_cart_message(cart)
    keyboard = get_cart_keyboard()

    await message.answer(cart_message, reply_markup=keyboard)


@dp.callback_query_handler(lambda c: c.data == "clear_cart", state="*")
async def clear_cart(callback_query: types.CallbackQuery, state: FSMContext):
    await state.update_data(cart={})
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🍽 Перейти к меню", callback_data="continue_shopping"))
    
    try:
        await callback_query.message.edit_text("🛒 Корзина очищена", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка редактирования сообщения: {e}")
        # Если сообщение нельзя отредактировать, отправляем новое
        await bot.send_message(
            callback_query.from_user.id,
            "🛒 Корзина очищена",
            reply_markup=keyboard
        )
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data == "checkout", state="*")
async def checkout(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get('cart', {})

    if not cart:
        try:
            await callback_query.message.edit_text("🛒 Ваша корзина пуста!")
        except Exception as e:
            logging.error(f"Ошибка редактирования сообщения: {e}")
            await bot.send_message(callback_query.from_user.id, "🛒 Ваша корзина пуста!")
        await callback_query.answer()
        return

    message_text = ("🕐 Введите время, к которому нужно приготовить заказ\n"
                   "Формат: ЧЧ:ММ (например, 13:30)")
    
    try:
        await callback_query.message.edit_text(message_text)
    except Exception as e:
        logging.error(f"Ошибка редактирования сообщения: {e}")
        await bot.send_message(callback_query.from_user.id, message_text)
    
    await OrderStates.choosing_time.set()
    await callback_query.answer()


@dp.message_handler(state=OrderStates.choosing_time)
async def process_time_selection(message: types.Message, state: FSMContext):
    try:
        time_str = message.text.strip()
        hour, minute = map(int, time_str.split(':'))

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Неверный формат времени")

        # Создаем время на сегодня
        pickup_time = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Если время уже прошло, переносим на завтра
        if pickup_time <= datetime.now():
            pickup_time += timedelta(days=1)

        await state.update_data(pickup_time=pickup_time)

        # Показываем подтверждение
        data = await state.get_data()
        cart = data.get('cart', {})
        cart_message = await format_cart_message(cart)

        confirmation_message = f"{cart_message}\n\n🕐 Время получения: {pickup_time.strftime('%H:%M')}\n\n❓ Подтвердить заказ?"

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_order"))
        keyboard.add(InlineKeyboardButton("❌ Отменить", callback_data="cancel_order"))

        await message.answer(confirmation_message, reply_markup=keyboard)
        await OrderStates.confirmation.set()

    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат времени. Пожалуйста, введите время в формате ЧЧ:ММ (например, 13:30)")


@dp.callback_query_handler(lambda c: c.data == "confirm_order", state=OrderStates.confirmation)
async def confirm_order(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get('cart', {})
    pickup_time = data.get('pickup_time')

    # Получаем пользователя
    user = await get_or_create_user(
        telegram_id=callback_query.from_user.id,
        username=callback_query.from_user.username
    )

    # Сохраняем заказ
    order = await save_order(user.id, cart, pickup_time)

    # Уведомляем администратора
    await notify_admin_about_order(order.id)

    # Очищаем состояние
    await state.finish()

    success_message = (f"✅ Заказ №{order.id} успешно оформлен!\n\n"
                      f"🕐 Время получения: {pickup_time.strftime('%H:%M')}\n"
                      f"📍 Заказ будет готов в указанное время.")
    
    try:
        await callback_query.message.edit_text(success_message)
    except Exception as e:
        logging.error(f"Ошибка редактирования сообщения: {e}")
        await bot.send_message(callback_query.from_user.id, success_message)
    
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data == "cancel_order", state=OrderStates.confirmation)
async def cancel_order(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🍽 Вернуться к меню", callback_data="continue_shopping"))
    
    try:
        await callback_query.message.edit_text("❌ Заказ отменен", reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка редактирования сообщения: {e}")
        await bot.send_message(
            callback_query.from_user.id,
            "❌ Заказ отменен",
            reply_markup=keyboard
        )
    
    await callback_query.answer()


@dp.message_handler(lambda message: message.text == "👤 Мои заказы")
async def show_my_orders(message: types.Message):
    user = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Order).options(
                selectinload(Order.order_items).selectinload(OrderItem.menu_item)
            ).where(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(5)
        )
        orders = result.scalars().all()

    if not orders:
        await message.answer("📝 У вас пока нет заказов")
        return

    orders_text = "📝 Ваши последние заказы:\n\n"
    for order in orders:
        status_emoji = "⏳" if order.status == "pending" else "✅"
        orders_text += f"{status_emoji} Заказ №{order.id}\n"
        orders_text += f"🕐 Время получения: {order.pickup_time.strftime('%H:%M')}\n"
        orders_text += f"📅 Создан: {order.created_at.strftime('%d.%m.%Y %H:%M')}\n"

        total_price = sum(item.menu_item.price * item.quantity for item in order.order_items)
        orders_text += f"💰 Сумма: {total_price}₸\n\n"

    await message.answer(orders_text)


# Административные обработчики
# @dp.callback_query_handler(lambda c: c.data.startswith('set_status:ready:'))
# async def set_order_ready(callback_query: types.CallbackQuery):


async def main():
    # Создаем таблицы при запуске
    await create_tables()

    # Запускаем бота
    await dp.start_polling()


if __name__ == '__main__':
    asyncio.run(main())
