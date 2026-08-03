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
pip install -r requirements.lock
pip install -e . --no-deps
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

## Setup de base de datos

**El esquema real vive en las migraciones del repo frontend**
([Ad-Scrapper](https://github.com/MrxHuaang/Ad-Scrapper), `supabase/migrations/`),
que se aplican con `supabase db push` — **nunca ejecutes DDL por el SQL Editor**
(regla del runbook de reconciliación del ledger). La tabla `profiles` de
producción ya existe con RLS y políticas seguras.

Si montas un proyecto Supabase desde cero SIN el repo frontend, la forma
mínima segura es:

```sql
CREATE TABLE IF NOT EXISTS profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id),
  plan TEXT DEFAULT 'free',
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own profile"
  ON profiles FOR SELECT
  TO authenticated
  USING (auth.uid() = id);
```

> ⚠️ Una versión anterior de este documento incluía
> `CREATE POLICY ... FOR ALL USING (true)` "para el service role". Esa
> política es **insegura y no debe usarse jamás**: sin `TO service_role`
> aplica a *todos* los roles, así que cualquier usuario con la anon key
> podría escribir su propio plan (auto-upgrade gratis). Nunca estuvo aplicada
> en producción. El service role **bypasea RLS por diseño** — no necesita
> ninguna política; basta con no crear políticas de escritura para los roles
> de la API. Si algún día se necesita una política explícita, debe llevar
> `TO service_role` y su `WITH CHECK`.

> La tabla `profiles` almacena el plan del usuario y los IDs de Stripe. La app la consulta en cada login para determinar el plan activo.

---

## Configurar Stripe (pagos)

1. Crea una cuenta en [stripe.com](https://stripe.com).
2. Ve a **Developers > API Keys** y copia la **Secret key** (`sk_test_...`) y la **Publishable key** (`pk_test_...`).
3. Crea los productos en el **Stripe Dashboard**:
   - **Products > Add product** > "Zephr Professional" > $29/mes > recurring.
   - Copia el **Price ID** (empieza con `price_`).
   - Repite para "Zephr Team" > $99/mes.
4. Configura el webhook — **el webhook canónico vive en el frontend Next.js,
   no en Flask** (el endpoint Flask `/api/stripe/webhook` devuelve 410 Gone
   para evitar procesar eventos duplicados):
   - **Developers > Webhooks > Add endpoint**.
   - URL: `https://ad-scrapper.vercel.app/api/stripe/webhook`
   - Eventos: los que consume `syncSubscriptionState` en el frontend
     (`checkout.session.completed`, `customer.subscription.updated`,
     `customer.subscription.deleted`, `invoice.payment_failed`, ...) — ver
     `docs/billing-lifecycle.md` del repo Ad-Scrapper.
   - Copia el **Webhook signing secret** (`whsec_...`) y configúralo en el
     proyecto de Vercel del FRONTEND.
5. Agrega las variables de entorno en Vercel (o `.env` local):

```
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRO_PRICE_ID=price_...
STRIPE_TEAM_PRICE_ID=price_...
BASE_URL=https://scrapping-avia.vercel.app   # en local: http://localhost:5000 (ver .env.example)
```

6. Redeploy en Vercel.
7. Ejecuta el SQL de `profiles` en Supabase (ver sección anterior).

### Rutas de Stripe

| Ruta | Método | Descripción |
|------|--------|-------------|
| `/api/stripe/config` | GET | Devuelve publishable key y planes |
| `/api/stripe/create-checkout-session` | POST | Crea sesión de checkout (requiere auth) |
| `/api/stripe/webhook` | POST | **Deprecado** — 410 Gone (el canonico vive en Next.js) |
| `/payment/success` | GET | Página de pago exitoso |
| `/payment/cancel` | GET | Página de pago cancelado |

### Probar pagos en modo test

Stripe proporciona tarjetas de prueba:
- **Pago exitoso**: `4242 4242 4242 4242` (cualquier fecha futura, cualquier CVC)
- **Pago rechazado**: `4000 0000 0000 0002`
- **Requiere autenticación**: `4000 0025 0000 3155`

---

## Solucion de problemas

- **"Auth no configurado"**: Verifica que `SUPABASE_URL` y `SUPABASE_ANON_KEY` estan definidas como variables de entorno.
- **Error al registrar**: Supabase requiere contrasenas de minimo 6 caracteres por defecto.
- **Google OAuth no funciona**: Verifica que el redirect URI en Google Cloud Console coincida exactamente con `https://tu-proyecto.supabase.co/auth/v1/callback`.
- **Email de confirmacion no llega**: Revisa la carpeta de spam. En desarrollo, puedes desactivar la confirmacion de email en **Authentication > Settings** en Supabase.
