# -*- coding: utf-8 -*-
{
    'name': "Catálogo de SKUs WB",
    'summary': "Aplicación principal para la gestión y filtro de SKUs",
    'description': """
        Módulo diseñado para reemplazar la app de Studio.
    """,
    'author': "Sergio Guerrero",
    'category': 'Inventory',
    'version': '18.0.1.0',
    'depends': ['base', 'product', 'stock'],
    'application': True,
    'sequence': 10,
    'data': [
        'views/catalogue_menus.xml',
        'views/catalogue_search.xml',
    ],
}