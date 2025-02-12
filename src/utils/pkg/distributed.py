import multiprocessing
import os
from collections import defaultdict
from functools import wraps

import torch as th
from torch import distributed as dist

from utils.basics import pickle_load, logger

cpu_group = None
gpu_group = None


def initialize_deepspeed(cfg):
    cfg.use_deepspeed = cfg.get('use_deepspeed', False) and th.cuda.is_available()
    cfg.use_fp16 = cfg.use_fp16 if cfg.use_deepspeed else False
    cfg.use_bf16 = cfg.use_bf16 if cfg.use_deepspeed else False
    if cfg.use_deepspeed:
        import deepspeed
        deepspeed.init_distributed(dist_backend='nccl', distributed_port=cfg.master_port)


def initialize_distributed(cfg, logger):
    cfg.is_distributed = False
    cfg.world_size = int(os.getenv('WORLD_SIZE', '1'))
    logger.info(f"world_size={cfg.world_size}")
    if th.cuda.is_available():
        cfg.master_ip = os.getenv('MASTER_ADDR', 'localhost')
        cfg.master_port = os.getenv('MASTER_PORT', '6000')
        cfg.local_rank = int(os.getenv('RANK', '0')) % th.cuda.device_count()
        device = cfg.local_rank % th.cuda.device_count()
        th.cuda.set_device(device)
        cfg.is_distributed = th.cuda.device_count() > 1
    else:
        cfg.local_rank = 0

    if cfg.world_size > 1 and not dist.is_initialized():
        logger.info(f"init_process_group")
        init_process_group("nccl", init_method="env://")  


def process_on_master_and_sync_by_pickle(cache_arg=None, cache_kwarg=None, log_func=logger.info):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if cache_kwarg is not None:
                filename = kwargs[cache_kwarg]
            elif cache_arg is not None:
                filename = args[cache_arg]
            else:
                log_func('No cache file specified')
            skip_cache = kwargs.pop('skip_cache', False)
            if not os.path.exists(filename) or skip_cache:
                if get_rank() == 0:  # Master process
                    func(*args, **kwargs)
            else:
                if get_rank() == 0:  # Master process
                    log_func(f'Loaded cache {filename}, skipped {func.__name__}')
                return pickle_load(filename)
            synchronize()
            assert os.path.exists(filename), f'The {filename} must be saved in the {func.__name__}'
            return pickle_load(filename)

        return wrapper

    return decorator


def master_process_only(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if get_rank() == 0:
            result = func(*args, **kwargs)
            return result
        synchronize()

    return wrapper


def get_rank():
    """
    Get the rank of this process in distributed processes.

    Return 0 for single process case.
    """
    if dist.is_initialized():
        return dist.get_rank()
    if "RANK" in os.environ:
        return int(os.environ["RANK"])
    return 0


def get_world_size():
    """
    Get the total number of distributed processes.

    Return 1 for single process case.
    """
    if dist.is_initialized():
        return dist.get_world_size()
    if "WORLD_SIZE" in os.environ:
        return int(os.environ["WORLD_SIZE"])
    return 1


def get_group(device):
    """
    Get the process group corresponding to the given device.

    Parameters:
        device (th.device): query device
    """
    group = cpu_group if device.type == "cpu" else gpu_group
    if group is None:
        raise ValueError("%s group is not initialized. Use comm.init_process_group() to initialize it"
                         % device.type.upper())
    return group


def init_process_group(backend, init_method=None, **kwargs):
    """
    Initialize CPU and/or GPU process groups.

    Parameters:
        backend (str): Communication backend. Use ``nccl`` for GPUs and ``gloo`` for CPUs.
        init_method (str, optional): URL specifying how to initialize the process group
    """
    global cpu_group
    global gpu_group

    dist.init_process_group(backend, init_method, **kwargs)
    gpu_group = dist.group.WORLD
    if backend == "nccl":
        cpu_group = dist.new_group(backend="gloo")
    else:
        cpu_group = gpu_group


def get_cpu_count():
    """
    Get the number of CPUs on this node.
    """
    return multiprocessing.cpu_count()


def synchronize():
    """
    Synchronize among all distributed processes.
    """
    if get_world_size() > 1:
        dist.barrier()


def _recursive_read(obj):
    values = defaultdict(list)
    sizes = defaultdict(list)
    if isinstance(obj, th.Tensor):
        values[obj.dtype] += [obj.flatten()]
        sizes[obj.dtype] += [th.tensor([obj.numel()], device=obj.device)]
    elif isinstance(obj, dict):
        for v in obj.values():
            child_values, child_sizes = _recursive_read(v)
            for k, v in child_values.items():
                values[k] += v
            for k, v in child_sizes.items():
                sizes[k] += v
    elif isinstance(obj, list) or isinstance(obj, tuple):
        for v in obj:
            child_values, child_sizes = _recursive_read(v)
            for k, v in child_values.items():
                values[k] += v
            for k, v in child_sizes.items():
                sizes[k] += v
    else:
        raise ValueError("Unknown type `%s`" % type(obj))
    return values, sizes


def _recursive_write(obj, values, sizes=None):
    if isinstance(obj, th.Tensor):
        if sizes is None:
            size = th.tensor([obj.numel()], device=obj.device)
        else:
            s = sizes[obj.dtype]
            size, s = s.split([1, len(s) - 1])
            sizes[obj.dtype] = s
        v = values[obj.dtype]
        new_obj, v = v.split([size, v.shape[-1] - size], dim=-1)
        # compatible with reduce / stack / cat
        new_obj = new_obj.view(new_obj.shape[:-1] + (-1,) + obj.shape[1:])
        values[obj.dtype] = v
        return new_obj, values
    elif isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            new_obj[k], values = _recursive_write(v, values, sizes)
    elif isinstance(obj, list) or isinstance(obj, tuple):
        new_obj = []
        for v in obj:
            new_v, values = _recursive_write(v, values, sizes)
            new_obj.append(new_v)
    else:
        raise ValueError("Unknown type `%s`" % type(obj))
    return new_obj, values


def reduce(obj, op="sum", dst=None):
    """
    Reduce any nested container of tensors.

    Parameters:
        obj (Object): any container object. Can be nested list, tuple or dict.
        op (str, optional): element-wise reduction operator.
            Available operators are ``sum``, ``mean``, ``min``, ``max``, ``product``.
        dst (int, optional): rank of destination worker. If not specified, broadcast the result to all workers.

    Example::

        >>> # assume 4 workers
        >>> rank = comm.get_rank()
        >>> x = th.rand(5)
        >>> obj = {"polynomial": x ** rank}
        >>> obj = comm.reduce(obj)
        >>> assert th.allclose(obj["polynomial"], x ** 3 + x ** 2 + x + 1)
    """
    values = _recursive_read(obj)[0]
    values = {k: th.cat(v) for k, v in values.items()}

    is_mean = op == "mean"
    if is_mean:
        op = "sum"
    op = getattr(dist.ReduceOp, op.upper())

    reduced = {}
    for k, v in values.items():
        dtype = v.dtype
        # NCCL can't solve bool. Cast them to byte
        if dtype == th.bool:
            v = v.byte()
        group = get_group(v.device)
        if dst is None:
            dist.all_reduce(v, op=op, group=group)
        else:
            dist.reduce(v, op=op, dst=dst, group=group)
        if is_mean:
            v = v / get_world_size()
        reduced[k] = v.type(dtype)

    return _recursive_write(obj, reduced)[0]


def stack(obj, dst=None):
    """
    Stack any nested container of tensors. The new dimension will be added at the 0-th axis.

    Parameters:
        obj (Object): any container object. Can be nested list, tuple or dict.
        dst (int, optional): rank of destination worker. If not specified, broadcast the result to all workers.

    Example::

        >>> # assume 4 workers
        >>> rank = comm.get_rank()
        >>> x = th.rand(5)
        >>> obj = {"exponent": x ** rank}
        >>> obj = comm.stack(obj)
        >>> truth = th.stack([th.ones_like(x), x, x ** 2, x ** 3]
        >>> assert th.allclose(obj["exponent"], truth))
    """
    values = _recursive_read(obj)[0]
    values = {k: th.cat(v) for k, v in values.items()}

    stacked = {}
    for k, v in values.items():
        dtype = v.dtype
        # NCCL can't solve bool. Cast them to byte
        if dtype == th.bool:
            dtype = th.uint8
        s = th.zeros(get_world_size(), *v.shape, dtype=dtype, device=v.device)
        s[get_rank()] = v
        group = get_group(s.device)
        if dst is None:
            dist.all_reduce(s, op=dist.ReduceOp.SUM, group=group)
        else:
            dist.reduce(s, op=dist.ReduceOp.SUM, dst=dst, group=group)
        stacked[k] = s.type(v.dtype)

    return _recursive_write(obj, stacked)[0]


def cat(obj, dst=None):
    """
    Concatenate any nested container of tensors along the 0-th axis.

    Parameters:
        obj (Object): any container object. Can be nested list, tuple or dict.
        dst (int, optional): rank of destination worker. If not specified, broadcast the result to all workers.

    Example::

        >>> # assume 4 workers
        >>> rank = comm.get_rank()
        >>> rng = th.arange(10)
        >>> obj = {"range": rng[rank * (rank + 1) // 2: (rank + 1) * (rank + 2) // 2]}
        >>> obj = comm.cat(obj)
        >>> assert th.allclose(obj["range"], rng)
    """
    values, sizes = _recursive_read(obj)
    sizes = {k: th.cat(v) for k, v in sizes.items()}

    sizes = stack(sizes)
    cated = {}
    for k, value in values.items():
        size = sizes[k].t().flatten()  # sizes[k]: (num_worker, num_obj)
        dtype = value[0].dtype
        # NCCL can't solve bool. Cast them to byte
        if dtype == th.bool:
            dtype = th.uint8
        s = th.zeros(size.sum(), dtype=dtype, device=value[0].device)
        obj_id = get_rank()
        world_size = get_world_size()
        offset = size[:obj_id].sum()
        for v in value:
            assert offset + v.numel() <= len(s)
            s[offset: offset + v.numel()] = v
            offset += size[obj_id: obj_id + world_size].sum()
            obj_id += world_size
        group = get_group(s.device)
        if dst is None:
            dist.all_reduce(s, op=dist.ReduceOp.SUM, group=group)
        else:
            dist.reduce(s, op=dist.ReduceOp.SUM, dst=dst, group=group)
        cated[k] = s.type(value[0].dtype)
    sizes = {k: v.sum(dim=0) for k, v in sizes.items()}

    return _recursive_write(obj, cated, sizes)[0]
