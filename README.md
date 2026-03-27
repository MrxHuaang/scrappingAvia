# AD Scraper - Airworthiness Directives

AD Scraper es una herramienta web integral construida en Python (Flask) que permite buscar y descargar **Airworthiness Directives (AD)** o Directivas de Aeronavegabilidad. 

Agrega resultados automáticos, consolidados y exportables desde las tres autoridades reguladoras más grandes:
- **Federal Register (FAA - Estados Unidos)**: Vía API Oficial del Registro Federal.
- **EASA (Unión Europea)**: Scraper de páginas oficial.
- **Transport Canada CAWIS (Canadá)**: Solución de web scraping simulando postbacks.
- **ANAC (Brasil)**: Scraper web del Sistema de Certificación DAE brasileño.

A través de esta interfaz puedes buscar los AD recientes, filtrar por palabras clave, fabricante o modelo para mantenerte al tanto de las reglas y regulaciones aéreas.

---

## ⚡ Requerimientos

- Python 3.8 o superior.
- Recomendación: usar un entorno virtual para las dependencias.

## 🛠️ Instalación y Uso

1. **Instala las dependencias:**
   Ejecuta el siguiente comando en la raíz del proyecto para instalar las librerías requeridas (Flask, BeautifulSoup, Requests, Pandas, Openpyxl, lxml).
   ```bash
   pip install -r requirements.txt
   ```

2. **Inicia el Servidor Web:**
   El proyecto levanta un servidor backend usando Flask y sirve la interfaz en el frontend.
   ```bash
   python app.py
   ```

3. **Accede a la herramienta:**
   Abre una pestaña en tu navegador web y visita: `http://127.0.0.1:5000`

---

## 💡 Ejemplos Funcionales de Búsqueda (Test)

Para validar que los tres orígenes están funcionando correctamente, puedes probar realizar las siguientes búsquedas a través de la interfaz visual.

### **1. Federal Register (USA)**
La FAA bloquea las herramientas automáticas en sus apartados de DRS (Dinamic Regulatory System). En esta aplicación la búsqueda para USA se delega al `api.federalregister.gov`, la fuente nativa de las regulaciones.

- **Prueba Sugerida:**
  - Selecciona la tarjeta **"Federal Register"**.
  - Escribe en **Fabricante**: `Boeing` o **Modelo**: `737`.
  - Dale en "Buscar". Podrás ver los números de la directiva y extraer los PDFs con un click.

### **2. EASA (Región Europea)**
EASA devuelve mucha información estructurada a través de su Safety Publications Tool.

- **Prueba Sugerida:**
  - Selecciona la tarjeta **"EASA"**.
  - Escribe en **Palabra clave/Fabricante**: `Airbus` o **Modelo**: `A320neo`.
  - Los resultados contarán con la fecha de la ejecución y el enlace visual al .pdf y si existe.
  - Alternativa sin fabricante: *Haz una búsqueda con los campos en blanco*, EASA traerá los ADs publicados recientemente esta semana.

### **3. Transport Canada (Canadá)**
Transport Canada utiliza una interfaz antigua CAWIS hecha en ASP.NET. El scraper se encarga de saltar la validación y traer la lista de manera automatizada.

- **Prueba Sugerida:**
  - Selecciona la tarjeta **"Transport Canada"**.
  - **Requiere escribir explícitamente el nombre de un fabricante** para funcionar, por ejemplo **Fabricante**: `Bombardier` y **Modelo**: `Global` o **Fabricante**: `Bell`.
  - Da click en buscar. Tomará un par de segundos mientras transacciona páginas internas del gobierno canadiense antes de traerte la lista exacta para el proveedor dado.

### **4. ANAC (Brasil)**
La Agência Nacional de Aviação Civil tiene su portal clásico DAE. Este scraper realiza navegación automatizada para resolver listas y extraer las Directivas de Aeronavegabilidad (DAs).

- **Prueba Sugerida:**
  - Selecciona la tarjeta **"ANAC Brasil"**.
  - Escribe en **Fabricante**: `Airbus` o `Boeing`.
  - Da click en buscar. Traerá los listados y las directrices (incluidas las provistas para empresas como Embraer si no filtras).

### **5. Vista "Todas las Fuentes"**
El botón global enviará las instrucciones a los cuatro apartados al mismo tiempo y listará los registros en una sola tabla, útil para partes o modelos que deban cumplir la múltiple directiva entre FAA, EASA, TC y ANAC.

---

## 🌗 Interfaz Clara y Oscura (Theme Toggle)

El frontend de este proyecto incluye un diseño moderno con soporte de Modo Oscuro (Por defecto) y **Modo Claro**. 
Puedes alternarlo haciendo clic en el ícono del **Sol / Luna** que encontrarás en la parte superior derecha de la interfaz, justo al lado de la palabra "Listo".

Tu preferencia quedará guardada en el `localStorage` de tal forma que al recargar la página siga viéndose optimizado bajo el mismo diseño.
