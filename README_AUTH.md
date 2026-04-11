# Autenticacion con Supabase — Zephr

Zephr usa **Supabase Auth** para login/registro. La app funciona sin configurar Supabase (modo demo), pero para activar la autenticacion real necesitas seguir estos pasos.

---

## 1. Crear un proyecto en Supabase

1. Ve a [https://supabase.com](https://supabase.com) y crea una cuenta gratuita.
2. Haz clic en **New Project**.
3. Elige un nombre, contrasena de base de datos y region (preferiblemente cercana a tus usuarios).
4. Espera a que el proyecto se aprovisione (1-2 minutos).

## 2. Obtener URL y Anon Key

1. En el dashboard de tu proyecto, ve a **Settings > API**.
2. Copia los siguientes valores:
   - **Project URL** — es tu `SUPABASE_URL` (ejemplo: `https://abcdefghij.supabase.co`)
   - **anon / public key** — es tu `SUPABASE_ANON_KEY` (una cadena larga tipo JWT)

> La **anon key** es segura para usar en el frontend. No expone datos sensibles por si sola, pero asegurate de configurar Row Level Security (RLS) en tus tablas.

## 3. Habilitar Google OAuth (opcional)

Para permitir login con Google:

1. En el dashboard de Supabase, ve a **Authentication > Providers**.
2. Busca **Google** y haz clic para habilitarlo.
3. Necesitaras crear credenciales OAuth en [Google Cloud Console](https://console.cloud.google.com/apis/credentials):
   - Crea un proyecto (o usa uno existente).
   - Ve a **APIs & Services > Credentials > Create Credentials > OAuth Client ID**.
   - Tipo de aplicacion: **Web application**.
   - En **Authorized redirect URIs** agrega:
     ```
     https://tu-proyecto.supabase.co/auth/v1/callback
     ```
   - Copia el **Client ID** y **Client Secret**.
4. Pega el Client ID y Client Secret en la configuracion de Google en Supabase.
5. Guarda los cambios.

## 4. Configurar variables de entorno

Crea un archivo `.env` en la raiz del proyecto (o usa variables de entorno del sistema):

```bash
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_ANON_KEY=tu-anon-key-aqui
FLASK_SECRET_KEY=una-clave-secreta-larga-y-aleatoria
```

> Puedes copiar `.env.example` como punto de partida:
> ```bash
> cp .env.example .env
> ```

### En produccion (Vercel, Railway, etc.)

Configura las mismas variables en el panel de variables de entorno de tu plataforma de hosting.

## 5. Instalar dependencias

Asegurate de tener el paquete `supabase` instalado:

```bash
pip install -r requirements.txt
```

## 6. Probar localmente

1. Configura las variables de entorno (archivo `.env` o exportalas manualmente).
2. Ejecuta la app:
   ```bash
   python app.py
   ```
3. Ve a `http://127.0.0.1:5000/register` para crear una cuenta.
4. Revisa tu email para confirmar la cuenta (Supabase envia un email de confirmacion por defecto).
5. Ve a `http://127.0.0.1:5000/login` para iniciar sesion.

### Modo demo (sin Supabase)

Si no configuras `SUPABASE_URL` y `SUPABASE_ANON_KEY`, la app muestra un banner indicando que auth esta en modo demo. Los formularios mostraran un mensaje de error amigable indicando que falta la configuracion.

---

## Rutas de autenticacion

| Ruta | Metodo | Descripcion |
|------|--------|-------------|
| `/auth/login` | POST | Login con email/contrasena |
| `/auth/register` | POST | Registro de nueva cuenta |
| `/auth/google` | GET | Inicia flujo OAuth con Google |
| `/auth/callback` | GET | Callback de OAuth (procesa tokens) |
| `/auth/logout` | POST | Cierra sesion |
| `/auth/me` | GET | Devuelve usuario actual |

## Solucion de problemas

- **"Auth no configurado"**: Verifica que `SUPABASE_URL` y `SUPABASE_ANON_KEY` estan definidas como variables de entorno.
- **Error al registrar**: Supabase requiere contrasenas de minimo 6 caracteres por defecto.
- **Google OAuth no funciona**: Verifica que el redirect URI en Google Cloud Console coincida exactamente con `https://tu-proyecto.supabase.co/auth/v1/callback`.
- **Email de confirmacion no llega**: Revisa la carpeta de spam. En desarrollo, puedes desactivar la confirmacion de email en **Authentication > Settings** en Supabase.
