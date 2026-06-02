# app/models/camila.py

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, JSON, Text, Numeric, Index, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime
import enum

from app.models.base import Base


class EstadoProcesamiento(enum.Enum):
    PENDIENTE = "pendiente"
    PROCESANDO = "procesando"
    COMPLETADO = "completado"
    ERROR = "error"


class TipoOperacion(enum.Enum):
    RECEPCION = "recepcion"
    ENTREGA = "entrega"
    CARGA = "carga"
    DESCARGA = "descarga"
    MIXTO = "mixto"


class TipoAsignacion(enum.Enum):
    REGULAR = "regular"
    EMERGENCIA = "emergencia"
    REPOSICIONAMIENTO = "reposicionamiento"


class ResultadoCamila(Base):
    """Resultado principal de una ejecución del modelo Camila"""
    __tablename__ = "resultados_camila"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escenario_id = Column(UUID(as_uuid=True), ForeignKey("escenarios.id", ondelete="CASCADE"), nullable=True)
    codigo = Column(String(50), unique=True, nullable=False, index=True)
    productividad_promedio_grua = Column(Numeric(10, 2), default=0, nullable=False)
    distribucion_carga_cv = Column(Numeric(10, 2), default=0, nullable=False)  # Coeficiente de variación
    factor_interferencia_pct = Column(Numeric(10, 2), default=0, nullable=False)
    indice_reasignaciones_pct = Column(Numeric(10, 2), default=0, nullable=False)
    tiempo_respuesta_pct = Column(Numeric(10, 2), default=0, nullable=False)  # % bloques sin grúa con demanda
    
    # Información temporal
    fecha_inicio = Column(DateTime, nullable=False, index=True)
    fecha_fin = Column(DateTime, nullable=False)
    anio = Column(Integer, nullable=False, index=True)
    semana = Column(Integer, nullable=False, index=True)
    dia = Column(Integer, nullable=False, index=True)  # 1-7
    turno = Column(Integer, nullable=False, index=True)  # 1-21
    turno_del_dia = Column(Integer, nullable=False)  # 1-3
    hora_inicio = Column(Integer, nullable=False, index=True)  # 0, 8, 16
    participacion = Column(Integer, nullable=False, index=True)  # 60-80
    con_dispersion = Column(Boolean, nullable=False, default=True, index=True)
    variant = Column(String(50), nullable=False, default="magdalena")
    criterio = Column(Integer, nullable=True)
    granularidad = Column(String(20), nullable=True)
    
    # Estado
    estado = Column(Enum(EstadoProcesamiento), default=EstadoProcesamiento.PROCESANDO, nullable=False)
    fecha_creacion = Column(DateTime, default=datetime.utcnow, nullable=False)
    fecha_procesamiento = Column(DateTime, nullable=True)
    
    # KPIs PRINCIPALES
    total_movimientos_modelo = Column(Integer, default=0, nullable=False)
    total_movimientos_real = Column(Integer, default=0, nullable=True)
    productividad_hora = Column(Numeric(10, 2), default=0, nullable=False)  # cont/hora
    productividad_meta_pct = Column(Numeric(10, 2), default=0, nullable=False)  # % sobre meta 75
    
    # KPIs PIPELINE
    min_diff_val = Column(Float, default=0, nullable=True)
    total_nRTG = Column(Integer, default=0, nullable=True)
    total_nRS = Column(Integer, default=0, nullable=True)

    # BALANCE DE FLUJOS
    total_entradas = Column(Integer, default=0, nullable=False)  # fr + fd
    total_salidas = Column(Integer, default=0, nullable=False)  # fc + fe
    balance_flujos = Column(Integer, default=0, nullable=False)  # entradas - salidas
    balance_categoria = Column(String(30), default='BALANCEADO')  # DESBALANCEADO_SALIDAS, etc
    
    # PRECISIÓN Y COMPARACIÓN
    precision_global = Column(Numeric(5, 2), nullable=True)  # % coincidencia modelo vs real
    score_coincidencia = Column(Numeric(5, 2), nullable=True)  # % de decisiones correctas
    ahorro_distancia_m = Column(Integer, default=0, nullable=True)  # metros ahorrados
    ahorro_tiempo_min = Column(Integer, default=0, nullable=True)  # minutos ahorrados
    ahorro_porcentual = Column(Numeric(5, 2), default=0, nullable=True)  # % de ahorro
    
    # UTILIZACIÓN
    utilizacion_bloques_pct = Column(Numeric(5, 2), default=0, nullable=False)
    bloque_mas_utilizado = Column(String(10), nullable=True)  # C1-C9
    concentracion_maxima_pct = Column(Numeric(5, 2), default=0, nullable=False)  # % del bloque más usado
    total_gruas_utilizadas = Column(Integer, default=0, nullable=False)
    utilizacion_gruas_pct = Column(Numeric(5, 2), default=0, nullable=False)
    
    # MÉTRICAS POR TIPO DE FLUJO
    movimientos_carga = Column(Integer, default=0, nullable=False)
    movimientos_descarga = Column(Integer, default=0, nullable=False)
    movimientos_entrega = Column(Integer, default=0, nullable=False)
    movimientos_recepcion = Column(Integer, default=0, nullable=False)
    
    # Metadata
    archivo_resultado = Column(String(255), nullable=True)
    archivo_instancia = Column(String(255), nullable=True)
    archivo_flujos_real = Column(String(255), nullable=True)
    
    # Relaciones
    escenario = relationship("app.models.optimization.Escenario", back_populates="resultados_camila")
    asignaciones_gruas = relationship("AsignacionGrua", back_populates="resultado", cascade="all, delete-orphan")
    cuotas_camiones = relationship("CuotaCamion", back_populates="resultado", cascade="all, delete-orphan")
    metricas_gruas = relationship("MetricaGrua", back_populates="resultado", cascade="all, delete-orphan")
    comparaciones = relationship("ComparacionDetallada", back_populates="resultado", cascade="all, delete-orphan")
    flujos_modelo = relationship("FlujoModelo", back_populates="resultado", cascade="all, delete-orphan")
    distribuciones_bloque = relationship("DistribucionBloque", back_populates="resultado", cascade="all, delete-orphan")
    recursos_bloque = relationship("ResumenRecursosBloque", back_populates="resultado", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index('idx_camila_temporal', 'anio', 'semana', 'dia', 'turno', 'hora_inicio'),
        Index('idx_camila_participacion', 'participacion'),
        Index('idx_camila_productividad', 'productividad_hora'),
        Index('idx_camila_precision', 'precision_global'),
    )


class DistribucionBloque(Base):
    """Distribución de actividad por bloque"""
    __tablename__ = "distribuciones_bloque"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)
    
    bloque_codigo = Column(String(10), nullable=False, index=True)
    movimientos_total = Column(Integer, default=0, nullable=False)
    porcentaje_actividad = Column(Numeric(5, 2), default=0, nullable=False)
    
    # Desglose por tipo
    movimientos_carga = Column(Integer, default=0, nullable=False)
    movimientos_descarga = Column(Integer, default=0, nullable=False)
    movimientos_entrega = Column(Integer, default=0, nullable=False)
    movimientos_recepcion = Column(Integer, default=0, nullable=False)
    
    # Comparación
    movimientos_real = Column(Integer, default=0, nullable=True)
    diferencia_absoluta = Column(Integer, default=0, nullable=True)
    diferencia_porcentual = Column(Numeric(10, 2), default=0, nullable=True)
    match_perfecto = Column(Boolean, default=False, nullable=False)
    
    # Pipeline
    min_diff_val = Column(Float, default=0, nullable=True)
    total_nRTG = Column(Integer, default=0, nullable=True)
    total_nRS = Column(Integer, default=0, nullable=True)
    
    resultado = relationship("ResultadoCamila", back_populates="distribuciones_bloque")
    
    __table_args__ = (
        Index('idx_dist_resultado_bloque', 'resultado_id', 'bloque_codigo'),
    )
    
class ResumenRecursosBloque(Base):
    """
    Nueva tabla para guardar nRTG, nRS, aRTG, aRS.
    Índice: (Bloque, Periodo)
    """
    __tablename__ = "resumen_recursos_bloque"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)

    bloque_codigo = Column(String(10), nullable=False, index=True)
    periodo = Column(Integer, nullable=False, index=True)

    # Cantidades Totales
    n_rtg = Column(Integer, default=0)
    n_rs = Column(Integer, default=0)

    # Cantidades Activas
    a_rtg = Column(Integer, default=0)
    a_rs = Column(Integer, default=0)

    # Relación de vuelta al padre
    resultado = relationship("ResultadoCamila", back_populates="recursos_bloque")

    __table_args__ = (
        Index('idx_resumen_bloque_periodo', 'resultado_id', 'bloque_codigo', 'periodo'),
    )

class ComparacionDetallada(Base):
    """Comparaciones detalladas modelo vs real"""
    __tablename__ = "comparaciones_detalladas"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)
    
    # Identificación
    tipo_comparacion = Column(String(50), nullable=False)  # 'FLUJO', 'BLOQUE', 'PERIODO', 'SEGREGACION'
    subtipo = Column(String(50), nullable=True)  # 'CARGA', 'C1', 'PERIODO_1', 'S1', etc
    
    # Valores
    valor_modelo = Column(Integer, default=0, nullable=False)
    valor_real = Column(Integer, default=0, nullable=True)
    match_exacto = Column(Boolean, default=False, nullable=False)
    
    # Análisis
    diferencia_absoluta = Column(Integer, default=0, nullable=True)
    diferencia_porcentual = Column(Numeric(10, 2), default=0, nullable=True)
    precision_pct = Column(Numeric(5, 2), default=0, nullable=True)
    
    # Categorización
    categoria_diferencia = Column(String(30), nullable=True)  # 'CRITICA', 'MODERADA', 'MENOR', 'PERFECTA'
    requiere_accion = Column(Boolean, default=False, nullable=False)
    
    resultado = relationship("ResultadoCamila", back_populates="comparaciones")
    
    __table_args__ = (
        Index('idx_comp_resultado_tipo', 'resultado_id', 'tipo_comparacion'),
        Index('idx_comp_categoria', 'categoria_diferencia', 'requiere_accion'),
    )


class AsignacionGrua(Base):
    """Asignación de grúas a bloques por periodo según el modelo"""
    __tablename__ = "asignaciones_gruas"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)
    
    tipo_equipo = Column(String(10), default='GENERICO', server_default='GENERICO', nullable=False)
    grua_id = Column(Integer, nullable=False, index=True)  # 1-12
    bloque_codigo = Column(String(10), nullable=False, index=True)
    periodo = Column(Integer, nullable=False, index=True)  # 1-8
    hora_real = Column(Integer, nullable=False)  # 0-23
    
    asignada = Column(Boolean, default=False, nullable=False)
    activada = Column(Boolean, default=False, nullable=False)
    movimientos_asignados = Column(Integer, default=0, nullable=False)
    tipo_asignacion = Column(Enum(TipoAsignacion), default=TipoAsignacion.REGULAR)
    
    resultado = relationship("ResultadoCamila", back_populates="asignaciones_gruas")
    
    __table_args__ = (
        Index('idx_asig_resultado_periodo', 'resultado_id', 'periodo'),
        Index('idx_asig_grua_bloque', 'grua_id', 'bloque_codigo'),
        Index('idx_asig_hora', 'hora_real'),
    )


class FlujoModelo(Base):
    """Flujos de contenedores según el modelo"""
    __tablename__ = "flujos_modelo"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)
    
    tipo_flujo = Column(String(10), nullable=False, index=True)  # fr, fe, fc, fd
    segregacion_codigo = Column(String(50), nullable=False, index=True)
    bloque_codigo = Column(String(10), nullable=False, index=True)
    periodo = Column(Integer, nullable=False, index=True)
    hora_real = Column(Integer, nullable=False, index=True)  # 0-23
    
    cantidad = Column(Integer, default=0, nullable=False)
    tipo_operacion = Column(Enum(TipoOperacion), nullable=False)
    
    # Comparación directa
    cantidad_real = Column(Integer, default=0, nullable=True)
    match_exacto = Column(Boolean, default=False, nullable=False)
    
    resultado = relationship("ResultadoCamila", back_populates="flujos_modelo")
    
    __table_args__ = (
        Index('idx_flujo_resultado_periodo', 'resultado_id', 'periodo'),
        Index('idx_flujo_tipo_bloque', 'tipo_flujo', 'bloque_codigo'),
        Index('idx_flujo_hora', 'hora_real'),
    )


class CuotaCamion(Base):
    """Cuotas de camiones por periodo y bloque"""
    __tablename__ = "cuotas_camiones"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)
    
    periodo = Column(Integer, nullable=False, index=True)
    hora_real = Column(Integer, nullable=False, index=True)
    bloque_codigo = Column(String(10), nullable=False, index=True)
    
    cuota_modelo = Column(Integer, default=0, nullable=False)
    capacidad_maxima = Column(Integer, nullable=False)
    gruas_asignadas = Column(Integer, default=0, nullable=False)
    
    # Valores reales
    movimientos_reales = Column(Integer, nullable=True)
    utilizacion_modelo_pct = Column(Numeric(5, 2), default=0, nullable=False)
    utilizacion_real_pct = Column(Numeric(7, 2), nullable=True)



    
    # Análisis
    brecha_absoluta = Column(Integer, default=0, nullable=True)
    cumplimiento_pct = Column(Numeric(7, 2), nullable=True)

    
    tipo_operacion = Column(Enum(TipoOperacion), default=TipoOperacion.MIXTO)
    segregaciones_incluidas = Column(JSON)
    
    resultado = relationship("ResultadoCamila", back_populates="cuotas_camiones")
    
    __table_args__ = (
        Index('idx_cuota_resultado_periodo', 'resultado_id', 'periodo'),
        Index('idx_cuota_bloque', 'bloque_codigo'),
        Index('idx_cuota_hora', 'hora_real'),
    )


class MetricaGrua(Base):
    """Métricas de desempeño por grúa"""
    __tablename__ = "metricas_gruas"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)
    
    grua_id = Column(Integer, nullable=False, index=True)
    
    # Métricas modelo
    movimientos_modelo = Column(Integer, default=0, nullable=False)
    movimientos_por_hora = Column(Numeric(10, 2), default=0, nullable=False)
    bloques_visitados = Column(Integer, default=0, nullable=False)
    periodos_activa = Column(Integer, default=0, nullable=False)
    
    # Productividad
    tiempo_productivo_hrs = Column(Numeric(10, 2), default=0, nullable=False)
    tiempo_improductivo_hrs = Column(Numeric(10, 2), default=0, nullable=False)
    utilizacion_pct = Column(Numeric(10, 2), default=0, nullable=False)
    
    # Comparación
    movimientos_reales_estimados = Column(Integer, nullable=True)
    diferencia_vs_real = Column(Integer, nullable=True)
    cumple_meta = Column(Boolean, default=False, nullable=False)  # >50 mov/grúa
    
    resultado = relationship("ResultadoCamila", back_populates="metricas_gruas")
    
    __table_args__ = (
        Index('idx_metrica_resultado_grua', 'resultado_id', 'grua_id'),
        Index('idx_metrica_productividad', 'utilizacion_pct'),
    )


class SegregacionMapping(Base):
    """Mapeo entre códigos de segregación del modelo y nombres reales"""
    __tablename__ = "segregaciones_mapping"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados_camila.id"), nullable=False)
    codigo = Column(String(10), nullable=False, index=True)
    nombre = Column(String(100), nullable=False)
    tipo = Column(String(20))
    size = Column(Integer)
    
    resultado = relationship("ResultadoCamila", backref="segregaciones_mapping")
    
    __table_args__ = (
        Index('idx_segregacion_resultado_codigo', 'resultado_id', 'codigo'),
    )


class ParametroCamila(Base):
    """Parámetros del modelo Camila"""
    __tablename__ = "parametros_camila"
    
    id = Column(Integer, primary_key=True)
    codigo = Column(String(20), unique=True, nullable=False)
    descripcion = Column(String(200))
    valor_default = Column(Numeric(10, 2), nullable=False)
    valor_actual = Column(Numeric(10, 2), nullable=False)
    unidad = Column(String(20))
    activo = Column(Boolean, default=True)
    fecha_actualizacion = Column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('idx_param_codigo', 'codigo'),
    )