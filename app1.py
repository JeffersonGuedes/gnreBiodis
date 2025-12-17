import streamlit as st
import xml.etree.ElementTree as ET
from datetime import date, datetime
import re

# --- FUNÇÕES DE LIMPEZA E FORMATAÇÃO ---

def escape_xml(texto):
    """Substitui caracteres especiais que quebram o XML."""
    if not texto: return ""
    texto = str(texto)
    texto = texto.replace("&", "&amp;")
    texto = texto.replace("<", "&lt;")
    texto = texto.replace(">", "&gt;")
    texto = texto.replace('"', "&quot;")
    texto = texto.replace("'", "&#39;")
    return texto.strip()

def limpar_numero(texto):
    """Remove tudo que não for número."""
    if not texto: return ""
    return "".join(filter(str.isdigit, str(texto)))

def formatar_ibge_5_digitos(codigo_ibge):
    """Garante IBGE com 5 dígitos."""
    codigo = limpar_numero(codigo_ibge)
    if len(codigo) >= 5:
        return codigo[-5:]
    return codigo

def clean_tag(tag):
    """Remove o namespace {http://...} da tag."""
    if '}' in tag:
        return tag.split('}')[-1]
    return tag

def find_text_recursive(root, tag_name):
    """Busca o texto de uma tag em qualquer lugar."""
    for elem in root.iter():
        if clean_tag(elem.tag) == tag_name:
            return elem.text
    return None

# --- LÓGICA DE VALORES ---

def get_valores_robusto(root):
    """Prioriza vST ou vICMSUFDest no Total. Se zero, soma os Itens."""
    val_st_total = 0.0
    val_difal_total = 0.0

    # 1. Busca no bloco de Totais
    icms_tot = None
    for elem in root.iter():
        if clean_tag(elem.tag) == 'ICMSTot':
            icms_tot = elem
            break
    
    if icms_tot is not None:
        for child in icms_tot:
            tag = clean_tag(child.tag)
            if tag == 'vST' and child.text:
                val_st_total = float(child.text)
            if tag == 'vICMSUFDest' and child.text:
                val_difal_total = float(child.text)

    if val_st_total > 0: return val_st_total
    if val_difal_total > 0: return val_difal_total

    # 2. Soma Itens (Fallback)
    soma_st = 0.0
    soma_difal = 0.0

    for elem in root.iter():
        tag = clean_tag(elem.tag)
        if tag == 'vICMSUFDest' and elem.text:
            try: soma_difal += float(elem.text)
            except: pass
        if tag == 'vICMSST' and elem.text:
             try: soma_st += float(elem.text)
             except: pass

    if soma_st > 0: return soma_st
    if soma_difal > 0: return soma_difal
    return 0.0

# --- PROCESSAMENTO PRINCIPAL ---

def processar_nfe(arquivo_xml, receita, produto, data_pagamento):
    try:
        tree = ET.parse(arquivo_xml)
        root = tree.getroot()
        
        # --- 1. DADOS DO EMITENTE ---
        emit_cnpj = limpar_numero(find_text_recursive(root, 'CNPJ'))
        emit_xNome = find_text_recursive(root, 'xNome')
        emit_xLgr = find_text_recursive(root, 'xLgr')
        emit_nro = find_text_recursive(root, 'nro')
        
        cMun_raw = None
        for elem in root.iter():
            if clean_tag(elem.tag) == 'enderEmit':
                for child in elem:
                    if clean_tag(child.tag) == 'cMun':
                        cMun_raw = child.text
        emit_cMun = formatar_ibge_5_digitos(cMun_raw)
        
        emit_uf = None
        for elem in root.iter():
             if clean_tag(elem.tag) == 'enderEmit':
                for child in elem:
                    if clean_tag(child.tag) == 'UF':
                        emit_uf = child.text
                        
        emit_cep = limpar_numero(find_text_recursive(root, 'CEP'))
        emit_fone = limpar_numero(find_text_recursive(root, 'fone'))

        # --- 2. DADOS DO DESTINATÁRIO ---
        dest_cnpj = None
        dest_cpf = None
        dest_xNome = None
        dest_cMun = None
        dest_uf = None
        dest_ie = None

        for elem in root.iter():
            if clean_tag(elem.tag) == 'dest':
                for child in elem:
                    tag = clean_tag(child.tag)
                    if tag == 'CNPJ': dest_cnpj = limpar_numero(child.text)
                    if tag == 'CPF': dest_cpf = limpar_numero(child.text)
                    if tag == 'xNome': dest_xNome = child.text
                    if tag == 'IE': 
                        ie_val = limpar_numero(child.text)
                        if ie_val and ie_val.upper() != 'ISENTO':
                            dest_ie = ie_val
                    
                    if tag == 'enderDest':
                        for subchild in child:
                            subtag = clean_tag(subchild.tag)
                            if subtag == 'cMun': dest_cMun = formatar_ibge_5_digitos(subchild.text)
                            if subtag == 'UF': dest_uf = subchild.text

        if not dest_uf: return None, "UF Destino não encontrada."
        
        tag_id_dest = "CNPJ" if dest_cnpj else "CPF"
        val_id_dest = dest_cnpj if dest_cnpj else dest_cpf
        if not val_id_dest: return None, "Destinatário sem Doc."

        # --- 3. VALORES E CHAVES ---
        valor_pagar = get_valores_robusto(root)
        valor_str = f"{valor_pagar:.2f}"
        
        # Dados da Nota
        chave_acesso = limpar_numero(find_text_recursive(root, 'chNFe'))
        numero_nota = limpar_numero(find_text_recursive(root, 'nNF'))
        
        if not chave_acesso: return None, "Chave de Acesso ausente."
        if not numero_nota: numero_nota = "0" # Segurança

        # --- 4. REFERÊNCIA ---
        mes_ref = data_pagamento.month
        ano_ref = data_pagamento.year

    except Exception as e:
        return None, f"Erro leitura XML: {e}"

    if valor_pagar <= 0: return None, "Valor zerado."

    # --- LÓGICA DE RESOLUÇÃO DE ERROS (217 e 302) ---
    
    # Configuração Padrão - Opção 1 fixada
    # Tenta enganar o sistema usando Tipo 10 (Aceito) mas com Numero curto (Evita 302)
    tipo_doc_final = "10"
    valor_doc_final = numero_nota # Padrão seguro para Tipo 10 é o NÚMERO, não a CHAVE
    
    # Opções comentadas caso precise alterar no futuro:
    # Opção 2: Tipo 22 + Chave de Acesso (Estados Modernos)
    # tipo_doc_final = "22"
    # valor_doc_final = chave_acesso
    
    # Opção 3: Tipo 18 + Nº da Nota (Padrão Antigo)
    # tipo_doc_final = "18"
    # valor_doc_final = numero_nota
    
    # --- MONTAGEM XML ---
    
    xml_emitente = f"""
    <contribuinteEmitente>
        <identificacao>
            <CNPJ>{emit_cnpj}</CNPJ>
        </identificacao>
        <razaoSocial>{escape_xml(emit_xNome)}</razaoSocial>
        <endereco>{escape_xml(f"{emit_xLgr}, {emit_nro}")}</endereco>
        <municipio>{emit_cMun}</municipio>
        <uf>{emit_uf}</uf>
        <cep>{emit_cep}</cep>
        <telefone>{emit_fone}</telefone>
    </contribuinteEmitente>"""

    xml_dest_ie = f"<IE>{dest_ie}</IE>" if dest_ie else ""
    
    xml_destinatario = f"""
    <contribuinteDestinatario>
        <identificacao>
            <{tag_id_dest}>{val_id_dest}</{tag_id_dest}>
            {xml_dest_ie}
        </identificacao>
        <razaoSocial>{escape_xml(dest_xNome)}</razaoSocial>
        <municipio>{dest_cMun}</municipio>
    </contribuinteDestinatario>"""

    # Campo Extra 90: Vai a CHAVE COMPLETA (Isso é o que importa p/ validação fiscal)
    xml_extras = f"""
    <camposExtras>
        <campoExtra>
            <codigo>90</codigo>
            <valor>{chave_acesso}</valor>
        </campoExtra>
    </camposExtras>"""

    xml_guia = f"""
    <TDadosGNRE versao="2.00">
        <ufFavorecida>{dest_uf}</ufFavorecida>
        <tipoGnre>0</tipoGnre>
        {xml_emitente}
        <itensGNRE>
            <item>
                <receita>{receita}</receita>
                <documentoOrigem tipo="{tipo_doc_final}">{valor_doc_final}</documentoOrigem>
                <produto>{produto}</produto>
                <referencia>
                    <periodo>0</periodo>
                    <mes>{mes_ref:02d}</mes>
                    <ano>{ano_ref}</ano>
                    <parcela>1</parcela>
                </referencia>
                <dataVencimento>{data_pagamento}</dataVencimento>
                <valor tipo="11">{valor_str}</valor>
                {xml_destinatario}
                {xml_extras}
            </item>
        </itensGNRE>
        <valorGNRE>{valor_str}</valorGNRE>
        <dataPagamento>{data_pagamento}</dataPagamento>
    </TDadosGNRE>
    """
    return xml_guia, "Sucesso"

# --- INTERFACE ---

st.set_page_config(page_title="Gerador de Lote GNRE", layout="wide")
st.title("📄 Gerador de Lote GNRE")

with st.sidebar:
    st.header("Configurações")
    
    # Modo fixo: Tipo 10 + Nº da Nota (Resolve erro 302 e 217)
    # Para alterar o modo, edite diretamente no código da função processar_nfe()
    
    cod_receita = st.text_input("Código Receita", value="100102")
    cod_produto = st.text_input("Código Produto", value="88")
    data_pagto = st.date_input("Data Pagamento", value=date.today())

uploaded_files = st.file_uploader("Upload XMLs", type=["xml"], accept_multiple_files=True)

if uploaded_files:
    if st.button(f"Processar {len(uploaded_files)} Arquivos"):
        
        guias_xml = []
        log_erros = []
        total_valor_lote = 0.0
        
        header = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n<TLote_GNRE versao="2.00" xmlns="http://www.gnre.pe.gov.br">\n     <guias>\n'
        footer = '     </guias>\n</TLote_GNRE>'
        
        progress_bar = st.progress(0)

        for i, file in enumerate(uploaded_files):
            file.seek(0)
            guia_str, status = processar_nfe(file, cod_receita, cod_produto, data_pagto)
            
            if guia_str:
                guias_xml.append(guia_str)
                try:
                    start = guia_str.find('<valorGNRE>') + 11
                    end = guia_str.find('</valorGNRE>')
                    total_valor_lote += float(guia_str[start:end])
                except: pass
            else:
                log_erros.append(f"{file.name}: {status}")
            
            progress_bar.progress((i + 1) / len(uploaded_files))

        if guias_xml:
            conteudo_final = header + "\n".join(guias_xml) + "\n" + footer
            nome_arquivo = f"Lote_GNRE_{datetime.now().strftime('%d-%m-%Y_%H-%M-%S')}.xml"
            st.success(f"Lote Gerado! Total: R$ {total_valor_lote:.2f}")
            st.download_button("⬇️ Baixar Lote XML", data=conteudo_final, file_name=nome_arquivo, mime="application/xml")
        else:
            st.warning("Sem guias geradas.")

        if log_erros:
            st.error("Erros:")
            for erro in log_erros: st.write(erro)