from app.modules.users import service as user_service


async def get_users() -> list[dict]:
    return await user_service.get_all()


async def get_user_by_id(user_id: str) -> dict:
    return await user_service.get_by_id(user_id)


async def block_user(user_id: str) -> dict:
    return await user_service.update_status(user_id, "blocked")


async def unblock_user(user_id: str) -> dict:
    return await user_service.update_status(user_id, "active")


async def delete_user(user_id: str) -> None:
    await user_service.delete_user(user_id)
