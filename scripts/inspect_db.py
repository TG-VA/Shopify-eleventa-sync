"""
Script para inspeccionar las tablas y columnas de Eleventa (Firebird).
"""
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
    
    # 1. Obtener todas las tablas de usuario
    cur.execute("SELECT RDB$RELATION_NAME FROM RDB$RELATIONS WHERE RDB$SYSTEM_FLAG = 0 ORDER BY RDB$RELATION_NAME")
    tables = [r[0].strip() for r in cur.fetchall()]
    print(f"\nTablas encontradas ({len(tables)}):")
    print(", ".join(tables))
    
    # 2. Buscar tablas relacionadas con ventas / tickets / movimientos
    keywords = ["VENTA", "TICKET", "DETALLE", "HISTORIAL", "MOVIMIENTO", "PRODUCTO", "ARTICULO"]
    relevant_tables = [t for t in tables if any(k in t.upper() for k in keywords)]
    
    print("\nEstructura de tablas relevantes:")
    for table in relevant_tables:
        cur.execute("""
            SELECT RDB$FIELD_NAME 
            FROM RDB$RELATION_FIELDS 
            WHERE RDB$RELATION_NAME = ? 
            ORDER BY RDB$FIELD_POSITION
        """, (table,))
        cols = [r[0].strip() for r in cur.fetchall()]
        print(f"\n-- {table} ({len(cols)} columnas):")
        print("   " + ", ".join(cols))
        
    con.close()

if __name__ == "__main__":
    main()
