# database.py
import asyncio
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

DB_PATH = os.getenv("DATABASE", "./shop.json")

DEFAULT_CATEGORIES = [
    "👗 Qizlar kiyimlari",
    "🧥 O‘g‘il bolalar kiyimlari",
    "🍼 Yangi tug‘ilganlar",
    "👟 Poyabzallar",
    "🧸 O‘yinchoqlar",
    "🎒 Aksessuarlar",
]

DEFAULT_MENU_ROWS = [
    ["👗 Qizlar kiyimlari", "🧥 O‘g‘il bolalar kiyimlari"],
    ["🍼 Yangi tug‘ilganlar", "👟 Poyabzallar"],
    ["🧸 O‘yinchoqlar", "🎒 Aksessuarlar"],
    ["/cart", "📞 Aloqa", "ℹ️ Ma'lumot"],
]


def _default_db() -> Dict[str, Any]:
    return {
        "meta": {
            "next_product_id": 1,
            "next_order_id": 1,
            "next_order_item_id": 1,
        },
        "products": [],
        "orders": [],
        "order_items": [],
        "settings": {
            "categories": list(DEFAULT_CATEGORIES),
            "menu_rows": [list(row) for row in DEFAULT_MENU_ROWS],
        },
    }


async def _read_db() -> Dict[str, Any]:
    if not os.path.exists(DB_PATH):
        await _write_db(_default_db())
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _read_db_sync)
    changed = False
    if "settings" not in data:
        data["settings"] = {
            "categories": list(DEFAULT_CATEGORIES),
            "menu_rows": [list(row) for row in DEFAULT_MENU_ROWS],
        }
        changed = True
    else:
        if "categories" not in data["settings"]:
            data["settings"]["categories"] = list(DEFAULT_CATEGORIES)
            changed = True
        if "menu_rows" not in data["settings"]:
            data["settings"]["menu_rows"] = [list(row) for row in DEFAULT_MENU_ROWS]
            changed = True
    if changed:
        await _write_db(data)
    return data


def _read_db_sync() -> Dict[str, Any]:
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


async def _write_db(data: Dict[str, Any]) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_db_sync, data)


def _write_db_sync(data: Dict[str, Any]) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def init_db():
    if not os.path.exists(DB_PATH):
        await _write_db(_default_db())
    else:
        await _read_db()


async def add_product(name, category, price, desc, photo):
    data = await _read_db()
    pid = data["meta"]["next_product_id"]
    data["meta"]["next_product_id"] += 1
    data["products"].append(
        {
            "id": pid,
            "name": name,
            "category": category,
            "price": price,
            "desc": desc,
            "photo": photo,
        }
    )
    await _write_db(data)
    return pid


async def list_products_by_category(category):
    data = await _read_db()
    rows: List[Tuple[Any, ...]] = []
    for product in data["products"]:
        if product["category"] == category:
            rows.append(
                (
                    product["id"],
                    product["name"],
                    product["price"],
                    product["desc"],
                    product["photo"],
                )
            )
    return rows


async def list_all_products(limit: Optional[int] = None):
    data = await _read_db()
    products = sorted(data["products"], key=lambda p: p["id"])
    if limit is not None:
        products = products[:limit]
    rows = []
    for product in products:
        rows.append(
            (
                product["id"],
                product["name"],
                product["category"],
                product["price"],
                product["desc"],
                product["photo"],
            )
        )
    return rows


async def get_product(pid):
    data = await _read_db()
    for product in data["products"]:
        if product["id"] == pid:
            return (
                product["id"],
                product["name"],
                product["category"],
                product["price"],
                product["desc"],
                product["photo"],
            )
    return None


async def update_product(pid, name=None, category=None, price=None, desc=None, photo=None):
    data = await _read_db()
    updated = False
    for product in data["products"]:
        if product["id"] == pid:
            if name is not None:
                product["name"] = name
            if category is not None:
                product["category"] = category
            if price is not None:
                product["price"] = price
            if desc is not None:
                product["desc"] = desc
            if photo is not None:
                product["photo"] = photo
            updated = True
            break
    if updated:
        await _write_db(data)
    return updated


async def delete_product(pid):
    data = await _read_db()
    before = len(data["products"])
    data["products"] = [p for p in data["products"] if p["id"] != pid]
    if len(data["products"]) != before:
        await _write_db(data)
        return True
    return False


async def create_order(user_id, fullname, address, phone, total):
    data = await _read_db()
    order_id = data["meta"]["next_order_id"]
    data["meta"]["next_order_id"] += 1
    order = {
        "id": order_id,
        "user_id": user_id,
        "fullname": fullname,
        "address": address,
        "phone": phone,
        "total": total,
        "status": "pending",
        "created_ts": datetime.utcnow().isoformat(),
    }
    data["orders"].append(order)
    await _write_db(data)
    return order_id


async def add_order_item(order_id, product_id, qty, price):
    data = await _read_db()
    order_item_id = data["meta"]["next_order_item_id"]
    data["meta"]["next_order_item_id"] += 1
    data["order_items"].append(
        {
            "id": order_item_id,
            "order_id": order_id,
            "product_id": product_id,
            "qty": qty,
            "price": price,
        }
    )
    await _write_db(data)


async def get_order(order_id):
    data = await _read_db()
    for order in data["orders"]:
        if order["id"] == order_id:
            return (
                order["id"],
                order["user_id"],
                order["fullname"],
                order["address"],
                order["phone"],
                order["total"],
                order["status"],
                order["created_ts"],
            )
    return None


async def get_order_total(order_id):
    order = await get_order(order_id)
    return order[5] if order else None


async def list_orders(limit: int = 10):
    data = await _read_db()
    orders = sorted(data["orders"], key=lambda o: o["id"], reverse=True)
    result = []
    for order in orders[:limit]:
        result.append(
            (
                order["id"],
                order["user_id"],
                order["fullname"],
                order["address"],
                order["phone"],
                order["total"],
                order["status"],
                order["created_ts"],
            )
        )
    return result


async def get_order_items(order_id: int):
    data = await _read_db()
    items = []
    for item in data["order_items"]:
        if item["order_id"] == order_id:
            items.append(
                (
                    item["id"],
                    item["order_id"],
                    item["product_id"],
                    item["qty"],
                    item["price"],
                )
            )
    return items


async def update_order_status(order_id: int, status: str):
    data = await _read_db()
    updated = False
    for order in data["orders"]:
        if order["id"] == order_id:
            order["status"] = status
            updated = True
            break
    if updated:
        await _write_db(data)
    return updated


async def get_settings():
    data = await _read_db()
    return data["settings"]


async def set_categories(categories: List[str]):
    data = await _read_db()
    data["settings"]["categories"] = categories
    await _write_db(data)


async def set_menu_rows(menu_rows: List[List[str]]):
    data = await _read_db()
    data["settings"]["menu_rows"] = menu_rows
    await _write_db(data)
