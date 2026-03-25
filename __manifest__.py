# -*- coding: utf-8 -*-
{
    'name': "Catálogo de SKUs WB",
    'summary': "Aplicación principal para la gestión y filtro de SKUs",
    'description': """
        Módulo diseñado para reemplazar gestión de productos.
    """,
    'author': "Sergio Guerrero",
    'category': 'Inventory',
    'version': '18.0.1.0',
    'depends': ['base', 'product', 'stock', 'mrp'],
    'application': True,
    'sequence': 10,
    'data': [
        'security/security.xml',
        #'views/catalogue_menus.xml',
        'views/product_product_view.xml',
        'views/catalogue_search.xml',
        'data/cron.xml',
    ],
}