import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from marketplace.models import Product, ProductCategory, Shop


class Command(BaseCommand):
    help = 'Import Carrefour catalog rows from a JSON file into the Cache Mart shop.'

    def add_arguments(self, parser):
        default_path = Path(settings.BASE_DIR).parent / 'carefour-scrape' / 'products.json'
        parser.add_argument('--file', default=str(default_path))
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        try:
            with open(options['file'], encoding='utf-8') as catalog_file:
                rows = json.load(catalog_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'Could not read catalog JSON: {exc}') from exc

        if not isinstance(rows, list):
            raise CommandError('Catalog JSON must contain a list of products.')

        products = {}
        for index, row in enumerate(rows, start=1):
            try:
                product = self.normalize_row(row)
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise CommandError(f'Invalid product at row {index}: {exc}') from exc
            products.setdefault(product['source_id'], product)

        if not products:
            raise CommandError('No valid products found in catalog JSON.')

        shop = Shop.objects.filter(slug='cache-mart').first()
        if shop is None:
            raise CommandError('The Cache Mart shop (slug: cache-mart) does not exist.')

        categories = {product['category'] for product in products.values()}
        product_slugs = {product['slug'] for product in products.values()}
        existing_products = set(
            Product.objects.filter(shop=shop, slug__in=product_slugs).values_list('slug', flat=True)
        )
        new_count = len(product_slugs - existing_products)
        update_count = len(existing_products)

        if options['dry_run']:
            self.stdout.write(
                f'Dry run: {len(products)} unique products, {len(categories)} categories, '
                f'{new_count} new products, {update_count} updates.'
            )
            return

        created_categories = 0
        created_products = 0
        updated_products = 0
        with transaction.atomic():
            category_rows = {}
            for category_name in sorted(categories):
                category = ProductCategory.objects.filter(name__iexact=category_name).first()
                if category is None:
                    base_slug = slugify(category_name)[:120] or 'carrefour-products'
                    category_slug = base_slug
                    suffix = 2
                    while ProductCategory.objects.filter(slug=category_slug).exists():
                        category_slug = f'{base_slug[:115]}-{suffix}'
                        suffix += 1
                    category = ProductCategory.objects.create(
                        name=category_name,
                        slug=category_slug,
                    )
                    created_categories += 1
                category_rows[category_name] = category

            for product in products.values():
                _, created = Product.objects.update_or_create(
                    shop=shop,
                    slug=product['slug'],
                    defaults={
                        'category': category_rows[product['category']],
                        'name': product['name'],
                        'price': product['price'],
                        'image_url': product['image_url'],
                    },
                    create_defaults={
                        'category': category_rows[product['category']],
                        'name': product['name'],
                        'price': product['price'],
                        'image_url': product['image_url'],
                        'stock_quantity': 0,
                        'is_active': True,
                    },
                )
                if created:
                    created_products += 1
                else:
                    updated_products += 1

        self.stdout.write(self.style.SUCCESS(
            f'Imported {created_products} new and updated {updated_products} products; '
            f'created {created_categories} categories in {shop.name}.'
        ))
        if not shop.is_verified or shop.status != 'active':
            self.stdout.write(self.style.WARNING(
                'The Cache Mart shop is not publicly visible until it is active and verified.'
            ))

    @staticmethod
    def normalize_row(row):
        if not isinstance(row, dict):
            raise TypeError('each row must be an object')

        source_id = str(row['source_id']).strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', source_id):
            raise ValueError('source_id must contain only letters, digits, underscores, or hyphens')

        name = ' '.join(str(row['name']).split())[:160]
        category = ' '.join(str(row['category']).split())[:100]
        if not name or not category:
            raise ValueError('name and category are required')

        price = Decimal(str(row['price']).replace(',', '').strip()).quantize(Decimal('0.01'))
        if price <= 0:
            raise ValueError('price must be greater than zero')

        image_url = str(row['image_url']).strip()
        parsed_image = urlparse(image_url)
        if parsed_image.scheme != 'https' or parsed_image.hostname != 'cdn.mafrservices.com':
            raise ValueError('image_url must be an HTTPS Carrefour product image URL')

        base_slug = slugify(name)[:145] or 'carrefour-product'
        return {
            'source_id': source_id,
            'name': name,
            'category': category,
            'price': price,
            'image_url': image_url,
            'slug': f'{base_slug}-{source_id}'[:180],
        }