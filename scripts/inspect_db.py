import sys
from pathlib import Path

# Asegurar que el directorio raíz esté en sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fdb
from agent.config import settings

def main():
    print(f"Conectando a: {settings.ELEVENTA_DB_PATH} ...")
    con = fdb.connect(
        dsn=settings.ELEVENTA_DB_PATH,
        user=settings.ELEVENTA_DB_USER,
        password=settings.ELEVENTA_DB_PASS,
        charset="WIN1252"
    )
    cur = con.cursor()
    
    print("\n--- MUESTRA DE PRODUCTOS (3 registros) ---")
    cur.execute("SELECT FIRST 3 ID, CODIGO, DESCRIPCION, DINVENTARIO, USA_INVENTARIO FROM PRODUCTOS")
    for r in cur.fetchall():
        print(f"ID={r[0]} | CODIGO={r[1]} | DESC={r[2]} | DINVENTARIO={r[3]} | USA_INV={r[4]}")

    print("\n--- MUESTRA DE VENTATICKETS (3 últimos) ---")
    cur.execute("SELECT FIRST 3 ID, FOLIO, PAGADO_EN, TOTAL, ESTA_CANCELADO FROM VENTATICKETS ORDER BY ID DESC")
    for r in cur.fetchall():
        print(f"ID={r[0]} | FOLIO={r[1]} | PAGADO_EN={r[2]} | TOTAL={r[3]} | CANCELADO={r[4]}")

    print("\n--- MUESTRA DE VENTATICKETS_ARTICULOS (3 últimos) ---")
    cur.execute("SELECT FIRST 3 ID, TICKET_ID, PRODUCTO_CODIGO, PRODUCTO_NOMBRE, CANTIDAD, PAGADO_EN FROM VENTATICKETS_ARTICULOS ORDER BY ID DESC")
    for r in cur.fetchall():
        print(f"ID={r[0]} | TICKET_ID={r[1]} | CODIGO={r[2]} | NOMBRE={r[3]} | CANTIDAD={r[4]} | PAGADO_EN={r[5]}")

    print("\n--- TEST QUERY VENTAS RECIENTES ---")
    query = """
        SELECT 
            VTA.TICKET_ID,
            VTA.PRODUCTO_CODIGO,
            VTA.CANTIDAD,
            COALESCE(VTA.PAGADO_EN, VT.PAGADO_EN)
        FROM VENTATICKETS_ARTICULOS VTA
        JOIN VENTATICKETS VT ON VT.ID = VTA.TICKET_ID
        WHERE (VT.ESTA_CANCELADO IS NULL OR VT.ESTA_CANCELADO = 0)
        ORDER BY VTA.ID DESC
    """
    cur.execute(f"SELECT FIRST 3 {query[14:]}")
    for r in cur.fetchall():
        print(f"Ticket={r[0]} | Codigo={r[1]} | Cantidad={r[2]} | Fecha={r[3]}")

    con.close()
    print("\n¡Inspección completada con éxito!")

if __name__ == "__main__":
    main()
