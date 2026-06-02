# backend-terminal
1- Ejecutar 'docker compose up -d' estando en la ruta 'terminal-backend/'

2.1- Automáticamente se ejecuta un código de carga de datos ('scripts/docker-entrypoint.sh), aunque debido al gran tamaño de datos solo cargará la primera semana.
2.2- La carga de los datos puede tomar horas o aveces incluso fallar, por lo que se recomienda ejecutar 'docker compose logs -f backend' en la misma ruta para ver los logs en vivo del backend, mostrando el progreso de carga o errores.

3- Si la carga automática falló o se desea cargar más datos, se deben cargar manualmente con los scripts:
	- Ejecutar 'docker exec -it terminal_backend /bin/bash'

	- MODELOS OPTIMIZACION: Ejecutar 'python scripts/load_optimization_data.py'. Cargará todos los datos de todos los modelos, aunque se puede cargar por partes con:
		a) 'python scripts/load_optimization_data.py --variant magdalena'
		b) 'python scripts/load_optimization_data.py --variant pipeline'
		c) 'python scripts/load_optimization_data.py --variant e-constraint'
		d) 'python scripts/load_optimization_data.py --variant OTRA_VARIANTE' (si existen más variantes)
		e) Si algo falla, se recomienda cargar solo una fecha con 'python load_optimization_data.py --fecha 2022-01-03 --variant VARIANTE' para realizar debug.

	- CAMILA: Ejecutar 'python scripts/load_camila_data_complete.py --anio 2022 --participacion 68'
	(Es largo, tardó aproximadamente 40 minutos)

	- DATOS HISTÓRICOS: Ejecutar 'python scripts/load_historical_data.py'

	- SAI: Ejecutar 'python scripts/load_sai_data.py'

	- MOVEMENT FLOWS: Ejecutar 'python scripts/load_movement_flows.py'
	(Es una carga pesada: se completa correctamente con un sistema de 32GB de RAM. Hasta el momento no se ha podido con 16GB de RAM porque siempre termina abruptamente :/)

4- SI AUN ASÍ FALLA: 
	- Asegurar que la carpeta 'terminal-backend/data' posea los datos, ya que de ahí se cargarán. La organización de la carpeta 'data' al momento de esta redacción (24/05/2026) es:
		'''
		data/
		-camila/
			- 2022/
				-instancias_camila/
				-resultados_camila/
		-historico/
			- 2022/
			- Flujos.csv
			- resultados_CDT_expo_anio_SAI_2022.csv
			- resultados_CDT_impo_anio_SAI_2022.csv
			- resultados_congestion_SAI_2022.csv
			- resultados_TTT_expo_anio_SAI_2022.csv
			- resultados_TTT_impo_anio_SAI_2022.csv
		-modelos/
			- e-constraint/
				- resultados_generados_bahia_criterio_ii
					- instancias_camila/
					- instancias_magdalena/
					- resultados_camila/
					- resultados_magdalena/
			- magdalena/
				- 2022/
					- instancias_magdalena/
					- resultados_magdalena/
			- pipeline/
				- resultados_generados_bahia_criterio_ii/
					- instancias_camila/
					- instancias_magdalena/
					- resultados_camila/
					- resultados_magdalena/
				- resultados_generados_bahia_criterio_iii/
					- instancias_camila/
					- instancias_magdalena/
					- resultados_camila/
					- resultados_magdalena/
				- resultados_generados_pila_criterio_ii/
					- instancias_camila/
					- instancias_magdalena/
					- resultados_camila/
					- resultados_magdalena/
				- resultados_generados_pila_criterio_iii/
					- instancias_camila/
					- instancias_magdalena/
					- resultados_camila/
					- resultados_magdalena/
		'''
	- Si no funciona, contactar a los desarrolladores. Pedirle contacto a su superior.