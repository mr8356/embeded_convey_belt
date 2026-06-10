#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define DEV_PATH "/dev/conveyor_node0"

int main(void)
{
	int fd;
	char buf[256];

	fd = open(DEV_PATH, O_RDONLY);
	if (fd < 0) {
		fprintf(stderr, "open %s failed: %s\n", DEV_PATH, strerror(errno));
		return 1;
	}

	printf("waiting for conveyor events from %s\n", DEV_PATH);

	for (;;) {
		struct pollfd pfd = {
			.fd = fd,
			.events = POLLIN,
		};
		int ret = poll(&pfd, 1, -1);
		ssize_t n;

		if (ret < 0) {
			if (errno == EINTR)
				continue;
			fprintf(stderr, "poll failed: %s\n", strerror(errno));
			break;
		}

		if (!(pfd.revents & POLLIN))
			continue;

		n = read(fd, buf, sizeof(buf) - 1);
		if (n < 0) {
			fprintf(stderr, "read failed: %s\n", strerror(errno));
			break;
		}

		buf[n] = '\0';
		printf("%s", buf);
		fflush(stdout);
	}

	close(fd);
	return 1;
}
