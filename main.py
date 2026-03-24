"""
AD Scraper - Herramienta principal
Obtiene Airworthiness Directives de FAA, EASA y Transport Canada.
Filtra por modelo/tipo y exporta resultados a CSV.
"""

import os
import sys
import pandas as pd
from datetime import datetime

from scraper_faa import search_ads as faa_search, get_makes as faa_get_makes, get_models_for_make as faa_get_models, _create_session as faa_create_session
from scraper_easa import search_ads as easa_search, search_main_page as easa_main_page
from scraper_tc import search_ads as tc_search, get_manufacturers as tc_get_manufacturers


OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def print_banner():
    """Muestra el banner de la aplicación."""
    print("\n" + "=" * 60)
    print("   ✈️  AD Scraper - Airworthiness Directives")
    print("   Fuentes: FAA | EASA | Transport Canada")
    print("=" * 60)


def select_source():
    """Permite al usuario seleccionar la fuente."""
    print("\n📋 Selecciona la fuente:")
    print("  1. FAA (USA)")
    print("  2. EASA (Europa)")
    print("  3. Transport Canada")
    print("  4. Todas las fuentes")
    print("  0. Salir")

    while True:
        choice = input("\n➡️  Opción: ").strip()
        if choice in ("0", "1", "2", "3", "4"):
            return int(choice)
        print("❌ Opción inválida. Intenta de nuevo.")


def search_faa_interactive():
    """Búsqueda interactiva en FAA."""
    print("\n🇺🇸 === FAA DRS ===")

    # Crear sesión reutilizable
    session = faa_create_session()

    # Obtener fabricantes
    print("Obteniendo fabricantes disponibles...")
    makes, session = faa_get_makes(session)

    selected_make = None
    if makes:
        # Permitir búsqueda por texto
        search = input("\n🔍 Buscar fabricante (ej: Boeing, Airbus): ").strip()
        if search:
            matching = [m for m in makes if search.lower() in m.lower()]
            if matching:
                print(f"\nFabricantes encontrados ({len(matching)}):")
                for i, m in enumerate(matching[:20], 1):
                    print(f"  {i}. {m}")

                choice = input("\n➡️  Número del fabricante (o Enter para usar el primero): ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(matching):
                    selected_make = matching[int(choice) - 1]
                else:
                    selected_make = matching[0]
                print(f"✅ Fabricante: {selected_make}")
            else:
                print(f"❌ No se encontró '{search}'. Usando búsqueda directa.")
                return faa_search(make=search, session=session)
    else:
        search = input("🔍 Ingresa el fabricante: ").strip()
        if search:
            selected_make = search

    # Obtener modelos
    selected_model = None
    if selected_make:
        print(f"\nObteniendo modelos para {selected_make}...")
        models, session = faa_get_models(selected_make, session)
        if models:
            search = input(f"\n🔍 Buscar modelo (Enter para todos, hay {len(models)} modelos): ").strip()
            if search:
                matching = [m for m in models if search.lower() in m.lower()]
                if matching:
                    print(f"\nModelos encontrados ({len(matching)}):")
                    for i, m in enumerate(matching[:20], 1):
                        print(f"  {i}. {m}")

                    choice = input("\n➡️  Número del modelo (o Enter para usar el primero): ").strip()
                    if choice.isdigit() and 1 <= int(choice) <= len(matching):
                        selected_model = matching[int(choice) - 1]
                    else:
                        selected_model = matching[0]
                    print(f"✅ Modelo: {selected_model}")

    # Seleccionar tipo de producto
    product_type = None
    pt_choice = input("\n🔍 Tipo de producto (1=Aircraft, 2=Engine, 3=Propeller, Enter=Todos): ").strip()
    if pt_choice == "1":
        product_type = "Aircraft"
    elif pt_choice == "2":
        product_type = "Engine"
    elif pt_choice == "3":
        product_type = "Propeller"

    print("\n🔄 Buscando ADs en FAA...")
    return faa_search(make=selected_make, model=selected_model, product_type=product_type, session=session)


def search_easa_interactive():
    """Búsqueda interactiva en EASA."""
    print("\n🇪🇺 === EASA ===")

    keyword = input("🔍 Buscar por modelo/fabricante (ej: A320, Boeing): ").strip()

    if not keyword:
        print("🔄 Obteniendo ADs recientes de EASA...")
        pages = input("📄 Número de páginas a obtener (default 3): ").strip()
        max_pages = int(pages) if pages.isdigit() else 3
        return easa_main_page(model_keyword=None, max_pages=max_pages)

    print(f"\n🔄 Buscando ADs para '{keyword}' en EASA...")
    return easa_search(model_keyword=keyword)


def search_tc_interactive():
    """Búsqueda interactiva en Transport Canada."""
    print("\n🇨🇦 === Transport Canada ===")

    manufacturer = input("🔍 Buscar fabricante (ej: Boeing, Airbus): ").strip()
    if not manufacturer:
        print("❌ Se requiere un fabricante para Transport Canada.")
        return []

    model = input("🔍 Buscar modelo (Enter para todos): ").strip() or None

    # Tipo de búsqueda
    print("\nTipo de búsqueda:")
    print("  1. Aircraft (default)")
    print("  2. Engine")
    print("  3. Propeller")
    st_choice = input("➡️  Opción: ").strip()

    search_type = "aircraft"
    if st_choice == "2":
        search_type = "engine"
    elif st_choice == "3":
        search_type = "propeller"

    print(f"\n🔄 Buscando ADs en Transport Canada...")
    return tc_search(manufacturer_name=manufacturer, model_name=model, search_type=search_type)


def export_results(results, filename=None):
    """Exporta los resultados a CSV."""
    if not results:
        print("⚠️  No hay resultados para exportar.")
        return None

    # Crear directorio de resultados
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ad_results_{timestamp}.csv"

    filepath = os.path.join(OUTPUT_DIR, filename)

    df = pd.DataFrame(results)

    # Ordenar columnas
    col_order = [
        "Source", "AD_Number", "Subject", "Make", "Model",
        "Effective_Date", "Status", "Product_Type", "Docket", "PDF_Link"
    ]
    existing_cols = [c for c in col_order if c in df.columns]
    extra_cols = [c for c in df.columns if c not in col_order]
    df = df[existing_cols + extra_cols]

    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    print(f"\n💾 Resultados exportados a: {filepath}")
    print(f"   Total registros: {len(df)}")

    # También exportar a Excel
    try:
        excel_path = filepath.replace(".csv", ".xlsx")
        df.to_excel(excel_path, index=False, engine="openpyxl")
        print(f"💾 También exportado a Excel: {excel_path}")
    except Exception as e:
        print(f"⚠️  No se pudo exportar a Excel: {e}")

    return filepath


def display_results(results, max_display=20):
    """Muestra los resultados en consola."""
    if not results:
        print("\n⚠️  No se encontraron resultados.")
        return

    print(f"\n📊 Resultados encontrados: {len(results)}")
    print("-" * 100)
    print(f"{'Source':<8} {'AD Number':<20} {'Make':<20} {'Model':<15} {'Date':<12} {'Subject':<30}")
    print("-" * 100)

    for r in results[:max_display]:
        source = r.get("Source", "")[:7]
        ad_num = r.get("AD_Number", "")[:19]
        make = r.get("Make", "")[:19]
        model = r.get("Model", "")[:14]
        date = r.get("Effective_Date", "")[:11]
        subject = r.get("Subject", "")[:29]
        print(f"{source:<8} {ad_num:<20} {make:<20} {model:<15} {date:<12} {subject:<30}")

    if len(results) > max_display:
        print(f"\n... y {len(results) - max_display} resultados más.")

    print("-" * 100)


def main():
    """Función principal."""
    print_banner()

    while True:
        source = select_source()

        if source == 0:
            print("\n👋 ¡Hasta luego!")
            break

        all_results = []

        if source in (1, 4):
            try:
                results = search_faa_interactive()
                all_results.extend(results)
            except Exception as e:
                print(f"❌ Error en FAA: {e}")

        if source in (2, 4):
            try:
                results = search_easa_interactive()
                all_results.extend(results)
            except Exception as e:
                print(f"❌ Error en EASA: {e}")

        if source in (3, 4):
            try:
                results = search_tc_interactive()
                all_results.extend(results)
            except Exception as e:
                print(f"❌ Error en Transport Canada: {e}")

        # Mostrar resultados
        display_results(all_results)

        # Exportar
        if all_results:
            export_choice = input("\n💾 ¿Exportar resultados a CSV? (s/n): ").strip().lower()
            if export_choice in ("s", "si", "sí", "y", "yes", ""):
                export_results(all_results)

        # Continuar?
        again = input("\n🔄 ¿Realizar otra búsqueda? (s/n): ").strip().lower()
        if again not in ("s", "si", "sí", "y", "yes"):
            print("\n👋 ¡Hasta luego!")
            break


if __name__ == "__main__":
    main()
