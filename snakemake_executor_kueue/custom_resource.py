from enum import Enum

from kubernetes import client, config
from snakemake.logging import logger

import snakemake_executor_kueue.utils as utils

config.load_kube_config()


class JobStatus(Enum):
    ACTIVE = 1
    FAILED = 3
    READY = 2
    SUCCEEDED = 4
    UNKNOWN = 5


class KubernetesObject:
    """
    Shared class and functions for Kubernetes object.
    """

    def __init__(self, job, executor_args):
        self.job = job
        self.executor_args = executor_args
        self.jobname = None

    def write_log(self, logfile):
        pass

    @property
    def jobprefix(self):
        """
        Derive the jobname from the associated job.
        """
        return ("snakejob-%s-%s" % (self.job.name, self.job.jobid)).replace("_", "-")


class BatchJob(KubernetesObject):
    """
    A default kubernetes batch job.
    """

    def status(self):
        """
        Get the status of the batch job.

        This should return one of four JobStatus. Note
        that we likely need to tweak the logic here.
        """
        batch_api = client.BatchV1Api()

        # This is providing the name, and namespace
        job = batch_api.read_namespaced_job(self.jobname, self.executor_args.namespace)

        # Any failure consider the job a failure
        if job.status.failed != 0:
            return JobStatus.FAILED

        # Any jobs either active or ready, we aren't done yet
        if job.status.active:
            return JobStatus.ACTIVE
        if job.status.ready:
            return JobStatus.READY

        # Have all completions succeeded?
        succeeded = job.status.succeeded
        if succeeded and succeeded == job.spec.completions:
            return JobStatus.SUCCEEDED
        return JobStatus.UNKNOWN

    def submit(self, job):
        """
        Receive the job back and submit it.

        This could easily be one function, but instead we are allowing
        the user to get it back (and possibly inspect) and then submit.
        """
        batch_api = client.BatchV1Api()
        result = batch_api.create_namespaced_job(self.executor_args.namespace, job)
        self.jobname = result.metadata.name
        return result

    def write_log(self, logprefix):
        """
        Write the job output to a logfile.

        Pods associated with a job will have a label for "jobname"
            metadata:
            labels:
               job-name: tacos46bqw
        """
        with client.ApiClient() as api_client:
            api = client.CoreV1Api(api_client)
            pods = api.list_namespaced_pod(
                namespace=self.executor_args.namespace,
                label_selector=f"job-name={self.jobname}",
            )
            for pod in pods.items:
                log = api.read_namespaced_pod_log(
                    name=pod.metadata.name, namespace=self.executor_args.namespace
                )
                logfile = f"{logprefix}-{pod.metadata.name}.txt"
                logger.debug(f"Writing output to {logfile}")
                utils.write_file(log, logfile)

    def generate(
        self,
        image,
        command,
        args,
        working_dir=None,
        deadline=None,
        environment=None,
    ):
        """
        Generate a CRD for a snakemake Job to run on Kubernetes.

        This function is intended for batchv1/Job, and we will eventually
        support others for the MPI Operator and Flux Operator.
        """
        deadline = self.job.resources.get("runtime")
        nodes = self.job.resources.get("_nodes")
        cores = self.job.resources.get("_cores")
        memory = self.job.resources.get("kueue.memory", "200Mi") or "200Mi"

        metadata = client.V1ObjectMeta(
            generate_name=self.jobprefix,
            labels={"kueue.x-k8s.io/queue-name": self.executor_args.queue_name},
        )

        environment = environment or {}
        environ = []
        for key, value in environment.items():
            environ.append({"name": key, "value": value})

        # Job container
        container = client.V1Container(
            image=image,
            name=self.jobprefix,
            command=[command],
            args=args,
            working_dir=working_dir,
            env=environ,
            resources={
                "requests": {
                    "cpu": cores,
                    "memory": memory,
                }
            },
        )

        if deadline:
            container.active_deadline_seconds = deadline

        # Job template
        template = {"spec": {"containers": [container], "restartPolicy": "Never"}}
        return client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=metadata,
            spec=client.V1JobSpec(
                parallelism=nodes, completions=3, suspend=True, template=template
            ),
        )


class FluxMiniCluster(KubernetesObject):
    """
    A Flux MiniCluster CRD
    """

    def submit(self, job):
        """
        Receive the job back and submit it.
        """
        crd_api = client.CustomObjectsApi()
        result = crd_api.create_namespaced_custom_object(
            group="flux-framework.org",
            version="v1alpha1",
            namespace=self.executor_args.namespace,
            plural="miniclusters",
            body=job,
        )
        self.jobname = result.metadata.name
        return result

    def generate(
        self,
        image,
        command,
        args,
        working_dir=None,
        deadline=None,
        environment=None,
    ):
        """
        Generate the MiniCluster crd.spec
        """
        import fluxoperator.models as models

        deadline = self.job.resources.get("runtime")
        cores = self.job.resources.get("_cores")
        nodes = self.job.resources.get("_nodes")
        memory = self.job.resources.get("kueue.memory", "200M1") or "200M1"
        tasks = self.job.resources.get("kueue.tasks", 1) or 1

        container = models.MiniClusterContainer(
            command=command + " " + " ".join(args),
            environment=environment,
            image=image,
            resources={
                "limits": {
                    "cpu": cores,
                    "memory": memory,
                }
            },
        )

        # For now keep logging verbose
        spec = models.MiniClusterSpec(
            job_labels={"kueue.x-k8s.io/queue-name": self.executor_args.queue_name},
            containers=[container],
            working_dir=working_dir,
            size=nodes,
            tasks=tasks,
            logging={"quiet": False},
        )
        if deadline:
            spec.deadline = deadline

        return models.MiniCluster(
            kind="MiniCluster",
            api_version="flux-framework.org/v1alpha1",
            metadata=client.V1ObjectMeta(
                generate_name=self.jobname,
                namespace=self.executor_args.namespace,
            ),
            spec=spec,
        )
