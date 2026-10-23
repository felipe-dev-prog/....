import os
import random
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import mercadopago

# =====================================================================
# CONFIGURAÇÕES E TOKENS
# =====================================================================
TOKEN = "8651947396:AAHk9AdfivhwO6lUqFvzeZiIK-WcPXIVyk4"
DATABASE_URL = os.environ.get("DATABASE_URL")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")

sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

server = Flask(__name__)
telegram_app = None

MAX_ENERGIA = 20
CUSTO_ENERGIA_ATAQUE = 2
TEMPO_REGEN_ENERGIA = 300  # 5 minutos por ponto

# =====================================================================
# GERENCIAMENTO DO BANCO DE DADOS (PostgreSQL / Supabase)
# =====================================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS jogadores (
                    user_id BIGINT PRIMARY KEY,
                    nome TEXT,
                    nivel INTEGER DEFAULT 1,
                    xp INTEGER DEFAULT 0,
                    ouro INTEGER DEFAULT 200,
                    energia INTEGER DEFAULT 20,
                    ultimo_regen BIGINT DEFAULT 0,
                    infantaria INTEGER DEFAULT 10,
                    arqueiros INTEGER DEFAULT 5,
                    cavalaria INTEGER DEFAULT 2,
                    vitorias INTEGER DEFAULT 0,
                    derrotas INTEGER DEFAULT 0,
                    vip_expiracao BIGINT DEFAULT 0,
                    vip_tipo INTEGER DEFAULT 0
                )
            """)
            conn.commit()

def carregar_ou_criar_jogador(user_id, nome):
    now = int(time.time())
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM jogadores WHERE user_id = %s", (user_id,))
            player = cursor.fetchone()

            if not player:
                cursor.execute("""
                    INSERT INTO jogadores (user_id, nome, energia, ultimo_regen)
                    VALUES (%s, %s, %s, %s)
                """, (user_id, nome, MAX_ENERGIA, now))
                conn.commit()
                cursor.execute("SELECT * FROM jogadores WHERE user_id = %s", (user_id,))
                player = cursor.fetchone()
            
            player_dict = dict(player)

            if player_dict['vip_expiracao'] > 0 and now > player_dict['vip_expiracao']:
                cursor.execute("""
                    UPDATE jogadores SET vip_expiracao = 0, vip_tipo = 0 WHERE user_id = %s
                """, (user_id,))
                conn.commit()
                player_dict['vip_expiracao'] = 0
                player_dict['vip_tipo'] = 0

            tempo_passado = now - player_dict['ultimo_regen']
            pontos_ganhos = tempo_passado // TEMPO_REGEN_ENERGIA

            if pontos_ganhos > 0 and player_dict['energia'] < MAX_ENERGIA:
                nova_energia = min(MAX_ENERGIA, player_dict['energia'] + pontos_ganhos)
                novo_ultimo_regen = now - (tempo_passado % TEMPO_REGEN_ENERGIA)
                
                cursor.execute("""
                    UPDATE jogadores SET energia = %s, ultimo_regen = %s WHERE user_id = %s
                """, (nova_energia, novo_ultimo_regen, user_id))
                conn.commit()
                
                player_dict['energia'] = nova_energia
                player_dict['ultimo_regen'] = novo_ultimo_regen

            return player_dict

def salvar_jogador(p):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE jogadores SET 
                    nivel = %s, xp = %s, ouro = %s, energia = %s, ultimo_regen = %s,
                    infantaria = %s, arqueiros = %s, cavalaria = %s,
                    vitorias = %s, derrotas = %s, vip_expiracao = %s, vip_tipo = %s
                WHERE user_id = %s
            """, (
                p['nivel'], p['xp'], p['ouro'], p['energia'], p['ultimo_regen'],
                p['infantaria'], p['arqueiros'], p['cavalaria'],
                p['vitorias'], p['derrotas'], p['vip_expiracao'], p['vip_tipo'],
                p['user_id']
            ))
            conn.commit()

def buscar_inimigos(user_id_atual, limite=5):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT user_id, nome, nivel, ouro FROM jogadores 
                WHERE user_id != %s ORDER BY RANDOM() LIMIT %s
            """, (user_id_atual, limite))
            return [dict(row) for row in cursor.fetchall()]

def buscar_jogador_por_id(user_id):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM jogadores WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None


# =====================================================================
# SISTEMA VIP E COMBATE
# =====================================================================
def obter_multiplicadores(p):
    if p['vip_expiracao'] > int(time.time()):
        if p['vip_tipo'] == 1:
            return 1.5, "VIP 7 Dias (1.5x)"
        elif p['vip_tipo'] == 2:
            return 2.0, "VIP 15 Dias (2.0x)"
        elif p['vip_tipo'] == 3:
            return 2.5, "VIP 30 Dias (2.5x)"
    return 1.0, "Padrão"

def calcular_poder_exercito(p):
    return (p['infantaria'] * 10) + (p['arqueiros'] * 15) + (p['cavalaria'] * 25)

def simular_combate(atacante, defensor):
    p_atacante = calcular_poder_exercito(atacante)
    p_defensor = calcular_poder_exercito(defensor)

    if atacante['cavalaria'] > defensor['arqueiros'] and defensor['arqueiros'] > 0:
        p_atacante *= 1.15
    if atacante['arqueiros'] > defensor['infantaria'] and defensor['infantaria'] > 0:
        p_atacante *= 1.15
    if atacante['infantaria'] > defensor['cavalaria'] and defensor['cavalaria'] > 0:
        p_atacante *= 1.15

    fator_sorte_atk = random.uniform(0.8, 1.2)
    fator_sorte_def = random.uniform(0.8, 1.2)

    total_atk = p_atacante * fator_sorte_atk
    total_def = p_defensor * fator_sorte_def

    vitoria = total_atk >= total_def

    taxa_perda_atk = random.uniform(0.05, 0.20) if vitoria else random.uniform(0.15, 0.35)
    taxa_perda_def = random.uniform(0.15, 0.35) if vitoria else random.uniform(0.05, 0.20)

    perdas_atk = {
        'infantaria': int(atacante['infantaria'] * taxa_perda_atk),
        'arqueiros': int(atacante['arqueiros'] * taxa_perda_atk),
        'cavalaria': int(atacante['cavalaria'] * taxa_perda_atk)
    }

    perdas_def = {
        'infantaria': int(defensor['infantaria'] * taxa_perda_def),
        'arqueiros': int(defensor['arqueiros'] * taxa_perda_def),
        'cavalaria': int(defensor['cavalaria'] * taxa_perda_def)
    }

    ouro_saqueado = 0
    if vitoria and defensor['ouro'] > 0:
        ouro_saqueado = int(defensor['ouro'] * random.uniform(0.2, 0.4))

    return vitoria, int(total_atk), int(total_def), perdas_atk, perdas_def, ouro_saqueado

def checar_level_up(p):
    xp_necessario = p['nivel'] * 100
    if p['xp'] >= xp_necessario:
        p['nivel'] += 1
        p['xp'] -= xp_necessario
        p['ouro'] += p['nivel'] * 50
        return True
    return False


# =====================================================================
# MENUS
# =====================================================================
def kb_menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏰 Meu Reino", callback_data="menu_reino"),
         InlineKeyboardButton("⚔️ Exército & Treino", callback_data="menu_exercito")],
        [InlineKeyboardButton("🛡️ Atacar Inimigos", callback_data="menu_atacar"),
         InlineKeyboardButton("💰 Coletar Recurso", callback_data="coletar")],
        [InlineKeyboardButton("💎 Loja VIP (Bônus)", callback_data="menu_vip"),
         InlineKeyboardButton("🏆 Ranking", callback_data="ranking")]
    ])

def kb_voltar():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Voltar ao Menu Principal", callback_data="menu_principal")]
    ])

def kb_treinamento():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡️ +5 Infantaria (50 Ouro)", callback_data="buy_infantaria")],
        [InlineKeyboardButton("🏹 +5 Arqueiros (80 Ouro)", callback_data="buy_arqueiros")],
        [InlineKeyboardButton("🐎 +2 Cavalaria (120 Ouro)", callback_data="buy_cavalaria")],
        [InlineKeyboardButton("🔙 Voltar", callback_data="menu_principal")]
    ])

def kb_loja_vip():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔹 VIP 7 Dias - R$ 9,90", callback_data="comprar_vip_1")],
        [InlineKeyboardButton("🔹 VIP 15 Dias - R$ 14,90", callback_data="comprar_vip_2")],
        [InlineKeyboardButton("🔹 VIP 30 Dias - R$ 25,90", callback_data="comprar_vip_3")],
        [InlineKeyboardButton("🔙 Voltar", callback_data="menu_principal")]
    ])


# =====================================================================
# HANDLERS DO TELEGRAM
# =====================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = carregar_ou_criar_jogador(user.id, user.first_name)
    _, nome_vip = obter_multiplicadores(p)
    
    texto = (
        f"⚔️ **BEM-VINDO AO SENHOR DA GUERRA!** ⚔️\n\n"
        f"Lorde **{p['nome']}**, o seu feudo aguarda suas ordens.\n"
        f"⚡ **Energia:** `{p['energia']}/{MAX_ENERGIA}` | 💰 **Ouro:** `{p['ouro']}`\n"
        f"✨ **Status VIP:** `{nome_vip}`"
    )
    await update.message.reply_text(texto, parse_mode="Markdown", reply_markup=kb_menu_principal())

async def cmd_darvip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uso: /darvip [user_id] [1=7d, 2=15d, 3=30d]")
        return
    
    target_id = int(args[0])
    tipo = int(args[1])
    alvo = buscar_jogador_por_id(target_id)
    if not alvo:
        await update.message.reply_text("❌ Jogador não encontrado.")
        return
    
    dias = 7 if tipo == 1 else 15 if tipo == 2 else 30
    alvo['vip_expiracao'] = int(time.time()) + (dias * 86400)
    alvo['vip_tipo'] = tipo
    salvar_jogador(alvo)
    
    await update.message.reply_text(f"✅ VIP de {dias} dias ativado com sucesso para {alvo['nome']}!")

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    p = carregar_ou_criar_jogador(user.id, user.first_name)
    data = query.data
    mult, nome_vip = obter_multiplicadores(p)

    if data == "menu_principal":
        texto = (
            f"⚔️ **SENHOR DA GUERRA - PAINEL DE COMANDO** ⚔️\n\n"
            f"Saudações, Lorde **{p['nome']}**.\n"
            f"⚡ **Energia:** `{p['energia']}/{MAX_ENERGIA}` | 💰 **Ouro:** `{p['ouro']}`\n"
            f"✨ **Status VIP:** `{nome_vip}`"
        )
        await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb_menu_principal())

    elif data == "menu_reino":
        poder_total = calcular_poder_exercito(p)
        expiracao_str = "Nenhum"
        if p['vip_expiracao'] > 0:
            expiracao_str = time.strftime('%d/%m/%Y %H:%M', time.localtime(p['vip_expiracao']))

        texto = (
            f"🏰 **RELATÓRIO DO FEUDO - {p['nome'].upper()}**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 **Nível do Lorde:** `{p['nivel']}` (XP: `{p['xp']}/{p['nivel']*100}`)\n"
            f"⚡ **Energia de Ação:** `{p['energia']}/{MAX_ENERGIA}`\n"
            f"💰 **Tesouro:** `{p['ouro']}` moedas de ouro\n"
            f"⚔️ **Poder Militar:** `{poder_total}` pts\n"
            f"✨ **Status VIP:** `{nome_vip}` (Expira em: {expiracao_str})\n\n"
            f"📊 **Batalhas:** 🥇 `{p['vitorias']}` Vitórias | 💀 `{p['derrotas']}` Derrotas"
        )
        await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb_voltar())

    elif data == "menu_vip":
        texto = (
            f"💎 **LOJA VIP - BÔNUS DE EXP E OURO** 💎\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Escolha um plano abaixo para gerar o seu Pix automático:"
        )
        await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb_loja_vip())

    elif data.startswith("comprar_vip_"):
        tipo_vip = int(data.split("_")[-1])
        valores = {1: (9.90, 7, "VIP 7 Dias"), 2: (14.90, 15, "VIP 15 Dias"), 3: (25.90, 30, "VIP 30 Dias")}
        preco, dias, nome_plano = valores[tipo_vip]

        payment_data = {
            "transaction_amount": preco,
            "description": f"Senhor da Guerra - {nome_plano} (User: {user.id})",
            "payment_method_id": "pix",
            "payer": {
                "email": f"jogador_{user.id}@senhordaguerra.com",
                "first_name": user.first_name[:20]
            },
            "external_reference": f"{user.id}_{tipo_vip}"
        }

        try:
            payment_response = sdk.payment().create(payment_data)
            payment = payment_response["response"]
            
            qr_code = payment["point_of_interaction"]["transaction_data"]["qr_code"]

            texto = (
                f"📲 **PIX GERADO COM SUCESSO!**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 **Item:** `{nome_plano}`\n"
                f"💵 **Valor:** `R$ {preco:.2f}`\n\n"
                f"Copia e cola o código Pix abaixo para pagar no teu banco:\n\n"
                f"`{qr_code}`\n\n"
                f"⚡ *O teu VIP será ativado automaticamente assim que o pagamento for aprovado!*"
            )
            await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb_voltar())
        except Exception as e:
            await query.edit_message_text(f"❌ Erro ao gerar Pix automático. Tente novamente mais tarde.", parse_mode="Markdown", reply_markup=kb_voltar())

    elif data in ["menu_exercito", "buy_infantaria", "buy_arqueiros", "buy_cavalaria"]:
        mensagem_feedback = ""
        if data == "buy_infantaria" and p['ouro'] >= 50:
            p['ouro'] -= 50
            p['infantaria'] += 5
            salvar_jogador(p)
            mensagem_feedback = "✅ Recrutados +5 Infantes!\n\n"
        elif data == "buy_arqueiros" and p['ouro'] >= 80:
            p['ouro'] -= 80
            p['arqueiros'] += 5
            salvar_jogador(p)
            mensagem_feedback = "✅ Recrutados +5 Arqueiros!\n\n"
        elif data == "buy_cavalaria" and p['ouro'] >= 120:
            p['ouro'] -= 120
            p['cavalaria'] += 2
            salvar_jogador(p)
            mensagem_feedback = "✅ Recrutados +2 Cavaleiros!\n\n"
        elif data != "menu_exercito":
            mensagem_feedback = "❌ Ouro insuficiente!\n\n"

        texto = (
            f"{mensagem_feedback}"
            f"🛡️ **QUARTEL E TROPAS MILITARES**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 **Ouro Atual:** `{p['ouro']}`\n\n"
            f"🛡️ Inf: `{p['infantaria']}` | 🏹 Arq: `{p['arqueiros']}` | 🐎 Cav: `{p['cavalaria']}`"
        )
        await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb_treinamento())

    elif data == "coletar":
        ganho_base = p['nivel'] * 25 + random.randint(10, 30)
        ganho_final = int(ganho_base * mult)
        p['ouro'] += ganho_final
        salvar_jogador(p)

        texto = f"💰 **IMPOSTOS COLETADOS!**\nRecolheu `{ganho_final}` moedas (`{mult}x`)."
        await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb_voltar())

    elif data == "menu_atacar":
        if p['energia'] < CUSTO_ENERGIA_ATAQUE:
            await query.edit_message_text(f"⚡ Energia insuficiente (`{p['energia']}/{MAX_ENERGIA}`).", parse_mode="Markdown", reply_markup=kb_voltar())
            return

        inimigos = buscar_inimigos(p['user_id'])
        if not inimigos:
            await query.edit_message_text("🏰 Não há reinos para atacar.", parse_mode="Markdown", reply_markup=kb_voltar())
            return

        keyboard_inimigos = [[InlineKeyboardButton(f"⚔️ {in_player['nome']} (Nível {in_player['nivel']})", callback_data=f"atacar_{in_player['user_id']}")] for in_player in inimigos]
        keyboard_inimigos.append([InlineKeyboardButton("🔙 Voltar", callback_data="menu_principal")])
        await query.edit_message_text("⚔️ **MESA DE GUERRA - ESCOLHA O ALVO**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard_inimigos))

    elif data.startswith("atacar_"):
        target_id = int(data.split("_")[1])
        defensor = buscar_jogador_por_id(target_id)
        if not defensor:
            await query.edit_message_text("❌ Reino inexistente.", reply_markup=kb_voltar())
            return

        p['energia'] -= CUSTO_ENERGIA_ATAQUE
        vitoria, poder_atk, poder_def, perdas_atk, perdas_def, ouro_roubado = simular_combate(p, defensor)

        p['infantaria'] = max(0, p['infantaria'] - perdas_atk['infantaria'])
        p['arqueiros'] = max(0, p['arqueiros'] - perdas_atk['arqueiros'])
        p['cavalaria'] = max(0, p['cavalaria'] - perdas_atk['cavalaria'])
        defensor['infantaria'] = max(0, defensor['infantaria'] - perdas_def['infantaria'])
        defensor['arqueiros'] = max(0, defensor['arqueiros'] - perdas_def['arqueiros'])
        defensor['cavalaria'] = max(0, defensor['cavalaria'] - perdas_def['cavalaria'])

        if vitoria:
            p['vitorias'] += 1
            defensor['derrotas'] += 1
            p['ouro'] += ouro_roubado
            defensor['ouro'] = max(0, defensor['ouro'] - ouro_roubado)
            xp_ganho = int((40 + (defensor['nivel'] * 10)) * mult)
            p['xp'] += xp_ganho
            subiu = checar_level_up(p)
            res = f"🔥 **VITÓRIA!**\nOuro: `+{ouro_roubado}` | XP: `+{xp_ganho}`"
            if subiu: res += f"\n🎉 **LEVEL UP! Nível {p['nivel']}!**"
        else:
            p['derrotas'] += 1
            defensor['vitorias'] += 1
            xp_ganho = int(10 * mult)
            p['xp'] += xp_ganho
            res = f"💀 **DERROTA!**\nXP de consolação: `+{xp_ganho}`"

        salvar_jogador(p)
        salvar_jogador(defensor)
        await query.edit_message_text(res, parse_mode="Markdown", reply_markup=kb_voltar())

    elif data == "ranking":
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT nome, nivel, vitorias FROM jogadores ORDER BY nivel DESC, vitorias DESC LIMIT 5")
                top_players = cursor.fetchall()
        texto = "🏆 **HALL DA FAMA - TOP 5**\n" + "\n".join([f"#{i} **{r['nome']}** - Nível `{r['nivel']}`" for i, r in enumerate(top_players, 1)])
        await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb_voltar())


# =====================================================================
# ROTA FLASK PARA WEBHOOK DE PAGAMENTO (MERCADO PAGO)
# =====================================================================
@server.route("/webhook/mercadopago", methods=["POST"])
def webhook_mp():
    data = request.json
    if not data:
        return jsonify({"status": "error"}), 400

    if data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        try:
            payment_info = sdk.payment().get(payment_id)
            payment = payment_info["response"]
            
            if payment.get("status") == "approved":
                ext_ref = payment.get("external_reference")
                if ext_ref:
                    user_id_str, tipo_str = ext_ref.split("_")
                    user_id = int(user_id_str)
                    tipo = int(tipo_str)

                    alvo = buscar_jogador_por_id(user_id)
                    if alvo:
                        dias = 7 if tipo == 1 else 15 if tipo == 2 else 30
                        alvo['vip_expiracao'] = int(time.time()) + (dias * 86400)
                        alvo['vip_tipo'] = tipo
                        salvar_jogador(alvo)

                        import asyncio
                        async def avisar_usuario():
                            from telegram import Bot
                            bot = Bot(token=TOKEN)
                            await bot.send_message(
                                chat_id=user_id,
                                text=f"🎉 **PAGAMENTO APROVADO!**\nO teu pacote VIP de `{dias} dias` foi ativado com sucesso no teu feudo!",
                                parse_mode="Markdown"
                            )
                        asyncio.run(avisar_usuario())
        except Exception as e:
            print(f"Erro ao processar webhook: {e}")

    return jsonify({"status": "ok"}), 200


# =====================================================================
# INICIALIZAÇÃO UNIFICADA (WEBHOOK + FLASK SEM CONFLITOS)
# =====================================================================
if __name__ == "__main__":
    init_db()
    
    PORT = int(os.environ.get("PORT", 8080))
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
    
    telegram_app = ApplicationBuilder().token(TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("darvip", cmd_darvip))
    telegram_app.add_handler(CallbackQueryHandler(callback_router))
    
    # Associa a aplicação Flask à aplicação do bot para partilhar a mesma porta sem conflitos
    telegram_app.bot_data["flask_app"] = server
    
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/{TOKEN}"
        print(f"🚀 Iniciando bot via Webhook em: {webhook_url}")
        
        # O run_webhook gerencia a porta de forma segura para o Render
        telegram_app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=webhook_url
        )
    else:
        print("🚀 Iniciando bot via Polling local...")
        telegram_app.run_polling()
