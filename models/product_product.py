from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import timedelta
import math
import logging

_logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# GUÍA RÁPIDA: product.template vs product.product
#
# product.template → "La ficha del producto"
#   Es el padre. Contiene el nombre, categoría, precio, descripción.
#   En Odoo siempre existe aunque el producto no tenga variantes.
#   Sus campos: id, name, categ_id, list_price, type, is_storable...
#   NO tiene product_tmpl_id (él mismo es el template).
#
# product.product → "La variante transaccional"
#   Es el hijo. Es el objeto real que se mueve: tiene barcode, SKU,
#   stock, id_product_madkting. Es lo que Yuju conoce y referencia.
#   Sus campos: id, product_tmpl_id (FK al template), default_code, barcode...
#   product_tmpl_id apunta hacia arriba al template padre.
#
# Relación: 1 template → N variantes (product.product)
#   Si no hay atributos (tallas, colores), Odoo crea 1 variante invisible
#   que comparte casi todo con el template — pero sigue siendo un objeto distinto.
#
# REGLA PRÁCTICA para este módulo:
#   Cuando self/record es product.template → usa record.id para el template
#   Cuando self/record es product.product  → usa record.product_tmpl_id.id para el template
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# ARQUITECTURA DE CATÁLOGO — 3 CASOS DE PRODUCTO
#
# CASO 1 — PRODUCTO SIMPLE (1:1):
#   Yuju crea:  SKU-ABC (salable_yuju, consu, sin rastreo)
#   Cron crea:  ALM-SKU-ABC (storable, con rastreo inventario)
#   BoM:        SKU-ABC → ALM-SKU-ABC (Phantom 1:1)
#   Stock:      stock(SKU-ABC) = free_qty(ALM-SKU-ABC)
#   Compras:    PO sobre ALM-SKU-ABC
#
# CASO 2 — COMBO REAL (vendibles compuestos):
#   Yuju crea:  COMBO-X (salable_yuju)
#   Yuju crea:  BoM con componentes → PROD-A, PROD-B (salable_yuju)
#   Cada componente es a su vez un simple (1:1):
#     PROD-A → ALM-PROD-A (storable)
#     PROD-B → ALM-PROD-B (storable)
#   Stock:      stock(COMBO-X) = min(stock(PROD-A), stock(PROD-B))
#               stock(PROD-A) = free_qty(ALM-PROD-A)  ← recursión
#   Compras:    PO sobre ALM-PROD-A, ALM-PROD-B individualmente
#
# CASO 3 — MULTICAJA (cajas no vendibles):
#   Yuju crea:  MULTI-X (salable_yuju)
#   Yuju crea:  BoM con componentes → MULTI-X#BOX1, MULTI-X#BOX2 (storable)
#   Componentes son storables puros. NO son vendibles individualmente.
#   NO son combos 1:1. No se les crea clon ni BoM adicional.
#   Stock:      stock(MULTI-X) = min(free_qty(BOX1)/qty1, free_qty(BOX2)/qty2)
#   Compras:    PO sobre MULTI-X#BOX1, MULTI-X#BOX2 (las cajas)
#   SKU patrón: {sku_padre}#BOX1, {sku_padre}#BOX2, etc.
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# MODELO: product.template
# Contiene la lógica de clasificación (data_entity_type) y los
# bloqueos de seguridad para evitar cambios destructivos en el catálogo.
# ══════════════════════════════════════════════════════════════════════════

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_internal_consu = fields.Boolean(
        string="Es Consumible Interno",
        default=False,
        help="Actívalo solo para insumos/consumibles internos. "
             "Si no, el producto almacenable se trata como Base.",
    )

    data_entity_type = fields.Selection(
        selection=[
            ('salable_yuju', 'Vendible (Yuju)'),
            ('storable', 'Almacenable Base'),
            ('internal_consu', 'Consumible Interno'),
        ],
        string="Rol del Producto",
        compute='_compute_data_entity_type',
        store=True,
        readonly=True,
        tracking=True,
    )

    @api.depends('type', 'is_storable', 'is_internal_consu')
    def _compute_data_entity_type(self):
        for record in self:
            if record.is_storable:
                if record.is_internal_consu:
                    record.data_entity_type = 'internal_consu'
                else:
                    record.data_entity_type = 'storable'
            else:
                record.data_entity_type = 'salable_yuju'

    def write(self, vals):
        locked_fields = {'type', 'is_storable'}

        if locked_fields & set(vals.keys()):
            for record in self:
                # Bloquear si intentan quitar is_storable a un producto con stock físico
                if 'is_storable' in vals and record.is_storable:
                    stock = record.qty_available
                    if stock > 0:
                        raise UserError(
                            f"[{record.name}] No se puede cambiar el tipo: "
                            f"tiene {stock} unidades en inventario físico."
                        )

                # Bloquear si intentan convertir un salable_yuju CON Kit en storable.
                if 'is_storable' in vals:
                    new_is_storable = vals.get('is_storable', record.is_storable)
                    if new_is_storable and not record.is_storable:
                        bom_count = self.env['mrp.bom'].search_count([
                            ('product_tmpl_id', '=', record.id)
                        ])
                        if bom_count > 0:
                            raise UserError(
                                f"[{record.name}] No se puede convertir a Almacenable: "
                                f"tiene {bom_count} Kit(s) activos. Elimínalos primero."
                            )

        return super().write(vals)


# ══════════════════════════════════════════════════════════════════════════
# MODELO: product.product
# Contiene:
#   1. Campos custom del catálogo
#   2. Lógica de estructura de producto (simple/combo/multibox)
#   3. Override de send_webhook_action() para propagar stock
#   4. Overrides del sistema de stock de Yuju con cálculo recursivo
#   5. Cron Job asíncrono para crear clones almacenables
# ══════════════════════════════════════════════════════════════════════════

class ProductProduct(models.Model):
    _inherit = 'product.product'

    # ── Campos custom del catálogo ────────────────────────────────────────

    data_product_id = fields.Char(
        string="Data Product ID",
        copy=False,
        index=True,
    )

    data_entity_type = fields.Selection(
        related='product_tmpl_id.data_entity_type',
        store=True,
        readonly=True,
        string="Rol del Producto",
    )

    is_internal_consu = fields.Boolean(
        related='product_tmpl_id.is_internal_consu',
        readonly=False,
    )

    # Vínculo explícito entre el vendible (salable_yuju) y su base física (storable).
    # Para productos simples (1:1): apunta al clon ALM-XXX.
    # Para combos/multicaja: NO se usa (la estructura vive en la BoM).
    storable_base_id = fields.Many2one(
        'product.product',
        string="Base Almacenable Vinculada",
        copy=False,
        domain=[('data_entity_type', '=', 'storable')],
    )

    # Relación inversa: desde una base almacenable, ver todos los vendibles que la usan
    salable_variant_ids = fields.One2many(
        'product.product',
        'storable_base_id',
        string="Vendibles Asociados",
        readonly=True,
    )

    # ── Estructura del producto ───────────────────────────────────────────
    # Clasifica el tipo de estructura comercial del vendible.
    # Solo aplica a salable_yuju; storables e internal_consu son False.

    product_structure = fields.Selection(
        selection=[
            ('simple', 'Simple (1:1)'),
            ('combo', 'Combo Real'),
            ('multibox', 'Multicaja'),
        ],
        string="Estructura de Producto",
        compute="_compute_product_structure",
        store=True,
        help="Simple: 1 vendible → 1 almacenable (BoM 1:1).\n"
             "Combo: vendible compuesto por otros vendibles individuales.\n"
             "Multicaja: vendible compuesto por cajas storables no vendibles.",
    )

    _sql_constraints = [
        ('data_product_id_uniq',
         'UNIQUE(data_product_id)',
         'El Data Product ID debe ser único por producto.'),
    ]

    # ══════════════════════════════════════════════════════════════════════
    # COMPUTE: product_structure
    #
    # Determina la estructura del vendible analizando su BoM:
    #   - Sin BoM o BoM 1:1 con un storable         → 'simple'
    #   - BoM con componentes salable_yuju           → 'combo'
    #   - BoM con componentes storable (#BOX patrón) → 'multibox'
    #   - No es salable_yuju                         → False
    # ══════════════════════════════════════════════════════════════════════

    @api.depends(
        'data_entity_type',
        'product_tmpl_id.bom_ids',
        'product_tmpl_id.bom_ids.bom_line_ids',
        'product_tmpl_id.bom_ids.bom_line_ids.product_id',
        'product_tmpl_id.bom_ids.bom_line_ids.product_qty',
    )
    def _compute_product_structure(self):
        for record in self:
            if record.data_entity_type != 'salable_yuju':
                record.product_structure = False
                continue

            bom = self._get_phantom_bom(record)

            if not bom or not bom.bom_line_ids:
                # Sin BoM todavía → asumimos simple (el cron la creará)
                record.product_structure = 'simple'
                continue

            lines = bom.bom_line_ids
            num_lines = len(lines)

            # BoM con un solo componente y qty=1 → simple (1:1)
            if num_lines == 1 and lines[0].product_qty == 1:
                component = lines[0].product_id
                if component.data_entity_type == 'storable':
                    record.product_structure = 'simple'
                    continue

            # Analizar componentes para distinguir combo vs multibox
            has_salable_components = any(
                line.product_id.data_entity_type == 'salable_yuju'
                for line in lines
            )
            has_box_components = any(
                self._is_box_sku(line.product_id.default_code, record.default_code)
                for line in lines
            )

            if has_box_components:
                record.product_structure = 'multibox'
            elif has_salable_components:
                record.product_structure = 'combo'
            else:
                # Todos los componentes son storables sin patrón #BOX
                # Podría ser un combo con storables directos o un multibox
                # sin el naming convention. Asumimos multibox por seguridad.
                if num_lines > 1 or any(l.product_qty > 1 for l in lines):
                    record.product_structure = 'multibox'
                else:
                    record.product_structure = 'simple'

    # ── Helpers ───────────────────────────────────────────────────────────

    def _get_phantom_bom(self, product):
        """
        Busca la BoM Phantom del vendible.
        Prioridad: específica de variante > genérica de template.
        """
        return self.env['mrp.bom'].search([
            ('type', '=', 'phantom'),
            '|',
            ('product_id', '=', product.id),
            '&',
                ('product_id', '=', False),
                ('product_tmpl_id', '=', product.product_tmpl_id.id),
        ], limit=1)

    @staticmethod
    def _is_box_sku(component_sku, parent_sku):
        """
        Detecta si un SKU de componente sigue el patrón de multicaja.
        Patrón: {parent_sku}#BOX1, {parent_sku}#BOX2, etc.

        También acepta variantes como #CAJA1, #PKG1 por flexibilidad.
        """
        if not component_sku or not parent_sku:
            return False
        component_upper = (component_sku or '').upper()
        parent_upper = (parent_sku or '').upper()
        if not component_upper.startswith(parent_upper + '#'):
            return False
        suffix = component_upper[len(parent_upper) + 1:]
        # Acepta BOX1, BOX2, CAJA1, PKG1, etc.
        return bool(suffix)

    # ══════════════════════════════════════════════════════════════════════
    # SECCIÓN: PROPAGACIÓN DE STOCK A COMBOS PADRE
    #
    # CONTEXTO — Cómo Yuju detecta cambios de stock:
    #
    #   1. stock.move.write({'state': 'done'}) ocurre en Odoo
    #   2. base.py del módulo madkting dispara el evento on_record_write
    #   3. listeners.py escucha stock.move con state IN ['assigned','done','cancel']
    #   4. El listener llama: record.product_id.send_webhook_action()
    #   5. send_webhook_action() calcula el stock y crea yuju.webhook.record
    #
    # PROBLEMA CON COMBOS Y MULTICAJA:
    #   El listener llama send_webhook_action() solo en el storable que movió.
    #   Los vendibles padres (combos, multicaja, simples 1:1) nunca se notifican.
    #
    # SOLUCIÓN:
    #   Overrideamos send_webhook_action(). Cuando se llama sobre un storable,
    #   buscamos todos los Kits Phantom que lo usan como componente y
    #   les disparamos su propio webhook. Funciona para los 3 casos:
    #     - Simple: ALM-X se mueve → VENDIBLE-X se notifica
    #     - Multibox: BOX1 se mueve → MULTI-X se notifica
    #     - Combo: ALM-X se mueve → PROD-X se notifica → COMBO-Y se notifica
    #       (la cadena recursiva se resuelve porque PROD-X también pasa por
    #        este override y propaga a sus padres)
    # ══════════════════════════════════════════════════════════════════════

    def send_webhook_action(self, auto_send=True, config=None):
        """
        Override de send_webhook_action del módulo madkting.

        Ejecuta el webhook normal y luego propaga a todos los vendibles
        que usan este producto como componente en alguna BoM Phantom.
        """
        # Ejecutar el webhook normal del producto que cambió
        result = super().send_webhook_action(auto_send=auto_send, config=config)

        # Solo propagamos si el producto que movió stock es un storable físico
        # O si es un salable_yuju que fue componente de un combo real.
        if self.data_entity_type not in ('storable', 'salable_yuju'):
            return result

        # Buscar todas las BoMs Phantom donde este producto aparece como componente
        bom_lines = self.env['mrp.bom.line'].search([
            ('product_id', '=', self.id),
            ('bom_id.type', '=', 'phantom'),
        ])

        if not bom_lines:
            return result

        # Recopilar los productos vendibles padre de todas las BoMs encontradas
        combos_padres = self.env['product.product']
        for line in bom_lines:
            bom = line.bom_id
            if bom.product_id:
                combos_padres |= bom.product_id
            else:
                combos_padres |= bom.product_tmpl_id.product_variant_ids

        # Filtrar solo los vendibles Yuju
        vendibles_afectados = combos_padres.filtered(
            lambda p: p.data_entity_type == 'salable_yuju'
        )

        # Disparar send_webhook_action en cada combo afectado
        for combo in vendibles_afectados:
            try:
                combo.send_webhook_action(auto_send=auto_send, config=config)
            except Exception as e:
                _logger.warning(
                    "Error propagando webhook a combo %s (id=%s): %s",
                    combo.default_code, combo.id, e
                )

        return result

    # ══════════════════════════════════════════════════════════════════════
    # SECCIÓN: OVERRIDES DEL SISTEMA DE STOCK DE YUJU
    #
    # Yuju tiene dos métodos para leer stock según el contexto:
    #
    # CAMINO A — _get_product_stock(product, location_ids, company_id)
    #   Usado en: send_webhook_action() — webhooks de stock individuales.
    #
    # CAMINO B — get_stock_products(products, location_ids, company_id)
    #   Usado en: send_webhook_all() — sincronizaciones masivas/batch.
    #
    # SOLUCIÓN para ambos caminos:
    #   Si el producto es salable_yuju, interceptamos y calculamos el
    #   stock virtual desde sus componentes via _calc_kit_stock().
    #   _calc_kit_stock es RECURSIVO: si un componente es otro salable_yuju
    #   (caso combo real), se resuelve recursivamente hasta llegar a storables.
    # ══════════════════════════════════════════════════════════════════════

    def _get_product_stock(self, product, location_ids, company_id):
        """
        Override del Camino A — webhooks individuales de stock.
        """
        if product.data_entity_type == 'salable_yuju':
            virtual_stock = self._calc_kit_stock(product, location_ids)
            locations = {str(loc): virtual_stock for loc in location_ids}
            stock_data = {
                "product_id": product.id,
                "company_id": company_id,
                "default_code": product.default_code,
                "stock": virtual_stock,
                "quantities": locations,
            }
            return stock_data, locations

        return super()._get_product_stock(product, location_ids, company_id)

    def get_stock_products(self, products, location_ids, company_id):
        """
        Override del Camino B — sincronizaciones batch/masivas.
        """
        vendibles = products.filtered(
            lambda p: p.data_entity_type == 'salable_yuju'
        )
        normales = products - vendibles

        result = []

        if normales:
            result = super().get_stock_products(normales, location_ids, company_id)

        for vendible in vendibles:
            virtual_stock = self._calc_kit_stock(vendible, location_ids)
            result.append({
                "product_id": vendible.id,
                "company_id": company_id,
                "default_code": vendible.default_code,
                "stock": virtual_stock,
                "quantities": {str(loc): virtual_stock for loc in location_ids},
            })

        return result

    @api.model
    def get_stock_data(self, location_id):
        """
        Override de la sincronización masiva inicial de Yuju.

        Incluimos tanto storables normales como vendibles salable_yuju
        en la búsqueda para que Yuju reciba stock de todos los productos.
        """
        config = self.env['madkting.config'].get_config()

        if config and config.webhook_product_mapped:
            return super().get_stock_data(location_id)

        product_ids = self.search([
            '|',
            ('is_storable', '=', True),
            ('data_entity_type', '=', 'salable_yuju'),
            ('default_code', '!=', False),
        ])

        company_id = config.company_id.id if config and config.company_id else None
        location_ids = self._get_location_ids(config, with_channels=False)

        stock_data = self.get_stock_products(
            products=product_ids,
            location_ids=location_ids,
            company_id=company_id,
        )

        return {
            'success': True,
            'data': [
                {
                    "product_id": str(el['product_id']),
                    "sku": el['default_code'],
                    "stock": el['stock'],
                }
                for el in stock_data
            ],
        }

    # ══════════════════════════════════════════════════════════════════════
    # NÚCLEO: _calc_kit_stock — Cálculo RECURSIVO de stock virtual
    #
    # Busca la BoM Phantom del vendible y calcula cuántas unidades
    # completas se pueden armar con el stock disponible de sus componentes.
    #
    # RECURSIÓN para Combos Reales:
    #   Si un componente es salable_yuju (es decir, es otro vendible
    #   que a su vez tiene su propia BoM 1:1), se resuelve recursivamente.
    #   Esto permite calcular: COMBO → PROD-A (salable) → ALM-PROD-A (storable)
    #
    # Fórmula para Kit simple 1:1:
    #   stock_virtual = free_qty(storable_base)
    #
    # Fórmula para Combo/Multicaja:
    #   stock_virtual = min( floor(stock_componente / qty_requerida) )
    #
    # Protección anti-ciclo:
    #   Se usa un set _visited para evitar recursión infinita si hay
    #   BoMs circulares mal configuradas.
    # ══════════════════════════════════════════════════════════════════════

    def _calc_kit_stock(self, product, location_ids, _visited=None):
        """
        Calcula stock virtual recursivo para productos Kit/Phantom.

        Args:
            product:      record product.product (el vendible)
            location_ids: lista de IDs de ubicación
            _visited:     set interno para protección anti-ciclo (no usar externamente)

        Returns:
            int: stock virtual disponible para venta (nunca negativo)
        """
        # ── Protección anti-ciclo ─────────────────────────────────────────
        if _visited is None:
            _visited = set()

        if product.id in _visited:
            _logger.warning(
                "Ciclo detectado en BoM al calcular stock de %s (id=%s). "
                "Revisa la configuración de Listas de Materiales.",
                product.default_code, product.id
            )
            return 0
        _visited.add(product.id)

        # ── Buscar BoM Phantom ────────────────────────────────────────────
        bom = self._get_phantom_bom(product)

        if not bom or not bom.bom_line_ids:
            # Sin BoM no podemos calcular stock virtual.
            # Esto puede pasar si el cron aún no creó la BoM 1:1.
            return 0

        available = float('inf')

        for line in bom.bom_line_ids:
            component = line.product_id

            if component.data_entity_type == 'salable_yuju':
                # ── CASO RECURSIVO (Combo Real) ───────────────────────────
                # El componente es otro vendible con su propia BoM.
                # Resolvemos recursivamente hasta llegar a storables.
                component_stock = self._calc_kit_stock(component, location_ids, _visited)
            else:
                # ── CASO BASE (Storable / Multibox Box) ───────────────────
                # El componente tiene stock físico real. Sumamos free_qty
                # en todas las ubicaciones configuradas.
                component_stock = sum(
                    component.with_context(location=loc).free_qty
                    for loc in location_ids
                )

            if line.product_qty > 0:
                available = min(available, math.floor(component_stock / line.product_qty))

        return int(available) if available != float('inf') else 0

    def get_vendible_available_stock(self, location_ids):
        """
        Calcula el stock disponible de un vendible directamente desde Odoo.
        Uso interno: reportes, pantallas custom, otros módulos del sistema.
        """
        self.ensure_one()
        if self.data_entity_type != 'salable_yuju':
            return 0
        return self._calc_kit_stock(self, location_ids)

    # ══════════════════════════════════════════════════════════════════════
    # SECCIÓN: CRON JOB — CREACIÓN AUTOMÁTICA DE BASES ALMACENABLES
    #
    # CONTEXTO:
    #   Cuando Yuju crea un producto en Odoo, siempre nace como salable_yuju
    #   (consu, sin rastreo de inventario). El proceso de duplicación NO puede
    #   hacerse sincrónicamente en el create(), porque los Combos y Multicajas
    #   hacen DOS llamadas separadas:
    #     1ª llamada: crea el SKU del producto
    #     2ª llamada: crea la BoM con los componentes (milisegundos después)
    #
    # SOLUCIÓN — Cron asíncrono con ventana de seguridad window_limit:
    #   El cron corre cada 5 minutos y busca vendibles recién creados
    #   que aún no tienen base creada (storable_base_id = False).
    #   La ventana de 10 minutos absorbe cualquier retraso de red de Yuju.
    #
    # LÓGICA DE DECISIÓN (3 casos):
    #
    #   SIN BoM → Es un PRODUCTO SIMPLE.
    #     Creamos: clon almacenable (ALM-sku) + BoM Phantom 1:1.
    #
    #   CON BoM + componentes con #BOX en SKU → Es MULTICAJA.
    #     No creamos nada. Los componentes son storables puros.
    #     Solo marcamos product_structure = 'multibox' (via compute).
    #     Marcamos los componentes como comprables para POs.
    #
    #   CON BoM + componentes salable_yuju → Es COMBO REAL.
    #     No creamos nada para el combo padre.
    #     Sus componentes individuales serán procesados por el cron
    #     en su propia iteración (cada componente es un simple 1:1).
    #     Solo marcamos product_structure = 'combo' (via compute).
    # ══════════════════════════════════════════════════════════════════════

    def _cron_create_storable_bases(self):
        """
        Punto de entrada del Cron Job de duplicación.

        Registrar en ir.cron con:
            model_id → product.product
            code     → model._cron_create_storable_bases()
            interval → 5 minutos
            active   → True
        """
        window_limit = fields.Datetime.now() - timedelta(minutes=10)
        wait_yuju = fields.Datetime.now() - timedelta(minutes=2)

        # Candidatos: vendibles recién creados sin base asignada todavía
        candidatos = self.env['product.product'].search([
            ('data_entity_type', '=', 'salable_yuju'),
            ('storable_base_id', '=', False),
            ('create_date', '<=', wait_yuju),
            ('create_date', '>=', window_limit),
        ])

        _logger.info(
            "Cron storable bases: %d candidatos encontrados en ventana de 10 min.",
            len(candidatos)
        )

        for vendible in candidatos:
            try:
                self._procesar_candidato(vendible)
            except Exception as e:
                _logger.error(
                    "Error procesando vendible %s (id=%s): %s",
                    vendible.default_code, vendible.id, e,
                    exc_info=True
                )

    def _procesar_candidato(self, vendible):
        """
        Procesa un vendible candidato según su estructura.

        Args:
            vendible: record product.product (salable_yuju sin storable_base_id)
        """
        # ¿Yuju ya creó una BoM para este vendible?
        bom_existente = self._get_phantom_bom(vendible)

        if not bom_existente:
            bom_existente = self.env['mrp.bom'].search([
                '|',
                ('product_id', '=', vendible.id),
                '&',
                    ('product_id', '=', False),
                    ('product_tmpl_id', '=', vendible.product_tmpl_id.id),
            ], limit=1)

        if bom_existente and bom_existente.bom_line_ids:
            # ── TIENE BoM: Combo Real o Multicaja ─────────────────────────
            self._procesar_con_bom(vendible, bom_existente)
        else:
            # ── SIN BoM: Producto Simple → crear clon 1:1 ────────────────
            _logger.info(
                "Producto simple detectado: %s (id=%s). Creando clon almacenable.",
                vendible.default_code, vendible.id
            )
            base = self._crear_clon_almacenable(vendible)
            self._crear_kit_phantom(vendible, base)

    def _procesar_con_bom(self, vendible, bom):
        """
        Procesa un vendible que ya tiene BoM (combo real o multicaja).

        No crea clones ni BoMs adicionales. Solo:
        - Detecta el tipo (combo vs multibox)
        - Para multibox: marca los componentes como comprables
        - Para combo: verifica que los componentes individuales tengan sus propias bases

        Args:
            vendible: record product.product (salable_yuju)
            bom:      record mrp.bom existente del vendible
        """
        lines = bom.bom_line_ids
        parent_sku = vendible.default_code or ''

        # Detectar si es multibox por patrón de SKU
        es_multibox = any(
            self._is_box_sku(line.product_id.default_code, parent_sku)
            for line in lines
        )

        if es_multibox:
            # ── CASO MULTICAJA ────────────────────────────────────────────
            _logger.info(
                "Multicaja detectado: %s (id=%s). %d cajas encontradas.",
                vendible.default_code, vendible.id, len(lines)
            )

            for line in lines:
                component = line.product_id
                # Asegurar que las cajas son comprables para POs
                if not component.purchase_ok:
                    component.purchase_ok = True
                    _logger.info(
                        "  Caja %s marcada como comprable (purchase_ok=True).",
                        component.default_code
                    )

            # Para multibox NO asignamos storable_base_id (no hay un solo base)
            # El campo product_structure se calculará automáticamente como 'multibox'

        else:
            # ── CASO COMBO REAL ───────────────────────────────────────────
            _logger.info(
                "Combo real detectado: %s (id=%s). %d componentes.",
                vendible.default_code, vendible.id, len(lines)
            )

            # Verificar que cada componente salable_yuju tenga su propia base
            for line in lines:
                component = line.product_id
                if component.data_entity_type == 'salable_yuju' and not component.storable_base_id:
                    # El componente aún no tiene su clon 1:1.
                    # Lo procesamos AHORA (puede que el cron no lo haya alcanzado aún).
                    component_bom = self._get_phantom_bom(component)
                    if not component_bom:
                        _logger.info(
                            "  Componente %s (id=%s) sin base. Creando clon 1:1.",
                            component.default_code, component.id
                        )
                        comp_base = self._crear_clon_almacenable(component)
                        self._crear_kit_phantom(component, comp_base)

        # Marcar el vendible con un storable_base_id simbólico
        # (primer storable encontrado, solo como referencia rápida)
        # Para combos: será el storable_base del primer componente
        # Para multibox: será la primera caja storable
        for line in lines:
            comp = line.product_id
            if comp.data_entity_type == 'storable':
                vendible.storable_base_id = comp
                break
            elif comp.data_entity_type == 'salable_yuju' and comp.storable_base_id:
                vendible.storable_base_id = comp.storable_base_id
                break

    def _crear_clon_almacenable(self, vendible):
        """
        Crea el producto físico (storable) espejo del vendible.

        Convenciones de nomenclatura:
            Nombre:  "[BASE] {nombre del vendible}"
            SKU:     "ALM-{sku del vendible}"

        Args:
            vendible: record product.product del vendible a clonar

        Returns:
            record product.product: el clon storable recién creado
        """
        sku_base = f"ALM-{vendible.default_code or vendible.id}"

        # Verificar si ya existe un storable con este SKU (idempotencia)
        existente = self.env['product.product'].search([
            ('default_code', '=', sku_base),
            ('data_entity_type', '=', 'storable'),
        ], limit=1)

        if existente:
            _logger.info(
                "  Clon almacenable ya existe: %s (id=%s). Reutilizando.",
                sku_base, existente.id
            )
            vendible.storable_base_id = existente
            return existente

        base = self.env['product.product'].create({
            'name': f"[BASE] {vendible.name}",
            'default_code': sku_base,
            'type': 'consu',
            'is_storable': True,
            'is_internal_consu': False,
            'categ_id': vendible.categ_id.id,
            'list_price': 0.0,
            'standard_price': vendible.standard_price,
            'sale_ok': False,       # La base NO se vende directamente
            'purchase_ok': True,    # La base SÍ se compra (POs)
            'barcode': False,       # Evitar duplicados de barcode
        })

        vendible.storable_base_id = base

        _logger.info(
            "  Clon almacenable creado: %s (id=%s) → base %s (id=%s)",
            vendible.default_code, vendible.id, base.default_code, base.id
        )

        return base

    def _crear_kit_phantom(self, vendible, base):
        """
        Crea la BoM tipo Kit (Phantom) que conecta el vendible con su base.

        Verifica que no exista ya una BoM para evitar duplicados.

        Args:
            vendible: record product.product del vendible
            base:     record product.product del clon storable
        """
        # Verificar idempotencia
        bom_existente = self.env['mrp.bom'].search([
            ('product_id', '=', vendible.id),
            ('type', '=', 'phantom'),
        ], limit=1)

        if bom_existente:
            _logger.info(
                "  BoM Phantom ya existe para %s (id=%s). Omitiendo creación.",
                vendible.default_code, vendible.id
            )
            return bom_existente

        bom = self.env['mrp.bom'].create({
            'product_tmpl_id': vendible.product_tmpl_id.id,
            'product_id': vendible.id,
            'type': 'phantom',
            'bom_line_ids': [(0, 0, {
                'product_id': base.id,
                'product_qty': 1.0,
            })],
        })

        _logger.info(
            "  BoM Phantom creada: vendible %s → base %s (bom_id=%s)",
            vendible.default_code, base.default_code, bom.id
        )

        return bom