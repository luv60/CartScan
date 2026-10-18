import asyncio
from blinkit_service import fetch_blinkit_products

async def main():
    products = await fetch_blinkit_products(
        query="milk",
        latitude=21.1468,
        longitude=79.0882,
        max_retries=0,
    )

    print("\nRESULT")
    print("products:", len(products))

    for p in products[:10]:
        print(
            p.get("name"),
            "| ?", p.get("price"),
            "|", p.get("packsize"),
            "|", p.get("product_id"),
        )

if __name__ == "__main__":
    asyncio.run(main())
